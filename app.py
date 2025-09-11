
import yaml
from pathlib import Path
from typing import Dict, Any, List

import gradio as gr
import pandas as pd
import plotly.graph_objects as go


CONFIG = Path('config.yml')


def load_config() -> Dict[str, Any]:
	with open(CONFIG, 'r') as fh:
		return yaml.safe_load(fh)


def seats_per_party(cfg: Dict[str, Any]) -> Dict[str, int]:
	"""Return seats tally per party (Pending Data if no winner)."""
	tally: Dict[str, int] = {}
	for c in cfg.get('constituencies', []):
		# Be tolerant of None/empty values
		seats = int(c.get('seats') or 0)
		winner = c.get('winner')
		# Use unified label 'Pending Data' for missing/undecided data
		valid = isinstance(winner, str) and winner.strip() and winner != 'Inconclusive'
		key = winner if valid else 'Pending Data'
		tally[key] = tally.get(key, 0) + seats
	return tally


def _party_color_map(cfg: Dict[str, Any]) -> Dict[str, str]:
	"""Return a stable color mapping per party name shared across charts.

	Order parties by seats tally (desc), with 'Pending Data' last, then assign colors.
	"""
	counts = seats_per_party(cfg)
	items = list(counts.items())
	items.sort(key=lambda kv: (kv[0] == 'Pending Data', -kv[1], kv[0]))
	palette = [
		'#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
		'#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
		'#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5',
		'#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5'
	]
	color_map: Dict[str, str] = {}
	for idx, (party, _cnt) in enumerate(items):
		color_map[party] = palette[idx % len(palette)]
	return color_map


def make_seats_stacked_bar(cfg: Dict[str, Any]) -> go.Figure:
	"""Render a single horizontal stacked bar where each segment is seats won by a party.

	No hardcoded parties; traces are generated dynamically from the config.
	"""
	counts = seats_per_party(cfg)
	if not counts:
		return go.Figure()

	# One bar category
	ycat = ['Seats']
	traces = []
	# Sort by descending count for readable legend
	# Sort with 'Pending Data' at the right-most (last), others by count desc
	items = list(counts.items())
	items.sort(key=lambda kv: (kv[0] == 'Pending Data', -kv[1]))
	colors = _party_color_map(cfg)
	for party, cnt in items:
		traces.append(
			go.Bar(
				x=[cnt],
				y=ycat,
				orientation='h',
				name=f"{party} ({cnt})",
				hovertemplate=f"{party}: %{{x}} seats<extra></extra>",
				marker=dict(color=colors.get(party))
			)
		)

	fig = go.Figure(traces)
	total_seats = sum(counts.values())
	fig.update_layout(
		barmode='stack',
		title='Seats by Party',
		height=240,  # thicker bar
		showlegend=True,
		margin=dict(l=40, r=20, t=40, b=10),
		legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0, traceorder='normal'),
		xaxis_title=f'All {total_seats} Seats',
	)
	# Show x-axis tick labels at the bottom and stretch to full width
	fig.update_xaxes(showticklabels=True, showgrid=False, showline=False, ticks='outside', range=[0, total_seats])
	fig.update_yaxes(showticklabels=False, showgrid=False, showline=False, ticks='')
	return fig


def build_results_table(cfg: Dict[str, Any]) -> pd.DataFrame:
	"""One-row-per-constituency table with CI details and spread.

	Columns: Constituency | Information | Possible Deviation

	- Information: multiline, one line per party in the form
	  "Party: (sample count). Confidence Interval: (Low, High)" using percentages.
	- Possible Deviation: constituency-level spread in percent if available; otherwise
	  falls back to the max per-party CI width.
	Constituencies with any missing sample_count are skipped.
	"""
	rows: List[Dict[str, Any]] = []

	for c in cfg.get('constituencies', []):
		cname = c.get('name')
		winner_val = c.get('winner')
		parties = c.get('parties', [])
		# Determine data availability
		has_missing = (not parties) or any(p.get('sample_count') is None for p in parties)
		# Compute total safely; treat None/empty as 0 to avoid int(None) before no_data handling
		total = sum(int(p.get('sample_count') or 0) for p in parties) if parties else 0
		no_data = has_missing or total == 0

		if no_data:
			rows.append({
				'Constituency': cname,
				'Winner': 'Pending Data',
				'Sample Count and Confidence Interval': 'Pending Data',
				'Possible Deviation': 'Pending Data',
			})
			continue

		info_lines: List[str] = []
		max_spread_pct = 0.0
		for p in parties:
			name = p.get('name')
			sc = int(p.get('sample_count') or 0)
			ci = p.get('confidence_interval')
			low_pct = high_pct = None
			if isinstance(ci, (list, tuple)) and len(ci) == 2:
				low_pct = float(ci[0]) * 100.0
				high_pct = float(ci[1]) * 100.0
				max_spread_pct = max(max_spread_pct, (high_pct - low_pct))
			low_str = f"{round(low_pct)}%" if low_pct is not None else "N/A"
			high_str = f"{round(high_pct)}%" if high_pct is not None else "N/A"
			info_lines.append(f"{name}: {sc}%. Confidence Interval: ({low_str}, {high_str})")

		spread_prop = c.get('spread')
		if isinstance(spread_prop, (int, float)):
			spread_pct = float(spread_prop) * 100.0
		else:
			spread_pct = max_spread_pct if max_spread_pct > 0 else None

		rows.append({
			'Constituency': cname,
			'Winner': (winner_val if winner_val else 'Pending Data'),
			'Sample Count and Confidence Interval': "\n".join(info_lines),
			'Possible Deviation': 'Pending Data' if spread_pct is None else f"{round(spread_pct, 2)}%",
		})

	df = pd.DataFrame(rows)
	# Order by arrival recency if present in config (latest first)
	seq_map = {c.get('name'): c.get('update_seq') for c in cfg.get('constituencies', [])}
	df['__seq'] = df['Constituency'].map(lambda n: seq_map.get(n) if seq_map else None)
	df.sort_values(by='__seq', ascending=False, inplace=True, na_position='last')
	df.drop(columns=['__seq'], inplace=True)
	return df


def make_constituency_stacked_pct(cfg: Dict[str, Any]) -> go.Figure:
	"""Optional: grouped by constituency stacked bar in percentages, dynamic parties.

	Returns a horizontal stacked bar where each row is a constituency and
	segments are party sample percentages. Constituencies with missing data are skipped.
	"""
	# Determine all parties present and order by total seats contested
	all_parties: List[str] = []
	# Precompute total seats contested per party across constituencies
	seats_by_party: Dict[str, int] = {}
	for c in cfg.get('constituencies', []):
		seats = int(c.get('seats') or 0)
		for p in c.get('parties', []) or []:
			name = p.get('name')
			if name is None:
				continue
			seats_by_party[name] = seats_by_party.get(name, 0) + seats
	data_rows: List[Dict[str, Any]] = []
	for c in cfg.get('constituencies', []):
		parties = c.get('parties', [])
		if any(p.get('sample_count') is None for p in parties):
			continue
		total = sum(int(p.get('sample_count') or 0) for p in parties) or 0
		if total == 0:
			continue
		row: Dict[str, Any] = {'Constituency': c.get('name')}
		for p in parties:
			name = p.get('name')
			if name not in all_parties:
				all_parties.append(name)
			pct = (int(p.get('sample_count') or 0) / total) * 100.0
			row[name] = pct
		data_rows.append(row)

	if not data_rows:
		return go.Figure()

	# Build traces per party
	ycats = [r['Constituency'] for r in data_rows][::-1]  # reverse for top-down
	traces = []
	# Sort parties like Popular Vote: Pending Data last, then seats contested desc, then national popular vote desc
	popular = cfg.get('popular_vote') or {}
	def _pv_sort_key(nm: str):
		prop = float(popular.get(nm, 0) or 0)
		return (nm == 'Pending Data', -seats_by_party.get(nm, 0), -prop)
	all_parties.sort(key=_pv_sort_key)
	for party in all_parties:
		xvals = [r.get(party, 0.0) for r in data_rows]
		traces.append(
			go.Bar(
				x=xvals,
				y=ycats,
				orientation='h',
				name=party,
				hovertemplate=f"{party}: %{{x:.2f}}%<extra></extra>",
			)
		)

	fig = go.Figure(traces)
	fig.update_layout(
		barmode='stack',
		title='Sample Share',
		height=max(300, 22 * len(ycats) + 80),
		margin=dict(l=40, r=20, t=40, b=40),
		legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0, traceorder='normal'),
		xaxis_title='Share of votes (%)',
	)
	# Stretch to full width (0-100%)
	fig.update_xaxes(range=[0, 100], showgrid=False, showline=False, ticks='')
	return fig

def make_pap_popular_vote_bar(cfg: Dict[str, Any]) -> go.Figure:
	"""Single stacked horizontal bar of popular vote (incl. Pending Data), 0–100%, colors match Seats by Party.

	Legend shows integer-rounded percentages; bars contain no text labels.
	"""
	popular = cfg.get('popular_vote') or {}
	if not isinstance(popular, dict) or not popular:
		# Fallback: only PAP (fill remainder as Pending Data so stack spans 100%)
		val = cfg.get('pap_popular_vote')
		pap_pct = max(0.0, min(100.0, float(val) * 100.0)) if isinstance(val, (int, float)) else 0.0
		items = [('PAP', pap_pct)]
		if pap_pct < 100.0:
			items.append(('Pending Data', 100.0 - pap_pct))
	else:
		# Build seats_by_party for ordering consistency; Pending Data last
		seats_by_party: Dict[str, int] = {}
		for c in cfg.get('constituencies', []):
			seats = int(c.get('seats') or 0)
			for p in c.get('parties', []) or []:
				nm = p.get('name')
				if nm is None:
					continue
				seats_by_party[nm] = seats_by_party.get(nm, 0) + seats
		items = list(popular.items())
		def sort_key(kv):
			name, prop = kv
			return (
				name == 'Pending Data',
				-seats_by_party.get(name, 0),
				-prop,
			)
		items.sort(key=sort_key)
		# Normalize to 100% just in case of rounding drift
		total_prop = sum(prop for _, prop in items) or 1.0
		items = [(name, (prop / total_prop) * 100.0) for name, prop in items]

	# Create one stacked bar across a single category
	ycat = ['Popular Vote']
	traces = []
	colors = _party_color_map(cfg)
	for name, pct in items:
		traces.append(
			go.Bar(
				x=[pct],
				y=ycat,
				orientation='h',
				name=f"{name} ({pct:.0f}%)",
				hovertemplate=f"{name}: %{{x:.1f}}%<extra></extra>",
				marker=dict(color=colors.get(name))
			)
		)

	fig = go.Figure(traces)
	fig.update_layout(
		barmode='stack',
		title='Popular Vote',
		height=240,
		showlegend=True,
		margin=dict(l=40, r=20, t=40, b=10),
		legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='left', x=0, traceorder='normal'),
		xaxis_title='Share of votes (%)',
	)
	fig.update_xaxes(range=[0, 100], showgrid=False, showline=False, ticks='')
	fig.update_yaxes(showticklabels=False, showgrid=False, showline=False, ticks='')
	return fig


def build_dashboard() -> gr.Blocks:
	cfg = load_config()
	seats_fig = make_seats_stacked_bar(cfg)
	pap_fig = make_pap_popular_vote_bar(cfg)
	table_df = build_results_table(cfg)
	const_pct_fig = make_constituency_stacked_pct(cfg)

	# Extract confidence level to show in the details table title
	conf_pct = None
	try:
		conf_pct = int(round(float(cfg.get('confidence_interval', 0)) * 100))
	except Exception:
		conf_pct = None
	details_label = (
		f"Statistics — Confidence: {conf_pct}%" if conf_pct is not None else "Statistics"
	)

	with gr.Blocks(title='GE2025 Election Sample Count Analysis') as demo:
		gr.Markdown("""<h1 style='text-align:center'>GE2025 Election Sample Count Analysis</h1>""")
		# Add next government line under the title
		next_gov = cfg.get('predicted_next_government')
		next_gov_str = next_gov if isinstance(next_gov, str) and next_gov.strip() else 'Inconclusive'
		gr.Markdown(f"""<h2 style='text-align:center'>Predicted Next Government: {next_gov_str}</h2>""")
		last_updated = cfg.get('last_updated')
		# quick elapsed for initial render
		try:
			import time as _t
			_delta = int(max(0, (_t.time() - int(last_updated)))) if last_updated else None
			if _delta is None:
				_elapsed_init = "Unknown"
			else:
				_mins, _secs = divmod(_delta, 60)
				_hrs, _mins = divmod(_mins, 60)
				if _hrs:
					_elapsed_init = f"{_hrs}h {_mins}m {_secs}s ago"
				elif _mins:
					_elapsed_init = f"{_mins}m {_secs}s ago"
				else:
					_elapsed_init = f"{_secs}s ago"
		except Exception:
			_elapsed_init = "Unknown"
		gr.Markdown(f"""<div style='text-align:center;color:#666'>Last updated: {_elapsed_init}</div>""")
		with gr.Row():
			gr.Plot(value=seats_fig)
		with gr.Row():
			gr.Plot(value=pap_fig)
		with gr.Row():
			gr.Dataframe(value=table_df, label=details_label, wrap=True, datatype=["markdown", "markdown", "markdown", "markdown"])
		with gr.Row():
			gr.Plot(value=const_pct_fig)

	return demo


if __name__ == '__main__':
	app = build_dashboard()
	app.launch()

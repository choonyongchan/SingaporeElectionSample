import yaml
import math
import time
import scipy.stats as st
from typing import Any, Dict, List, Tuple

def _require_keys(d: Dict[str, Any], keys: List[str], context: str) -> None:
    """Validate that a dict contains the listed keys.

    Raises a ValueError with a helpful message if any required key is missing.
    """
    for k in keys:
        if k not in d:
            raise ValueError(f"Missing required field '{k}' in {context}: {d}")


class ElectionCaller:
    """Analyze constituency poll samples and report confidence intervals and called seats.

    Configuration is provided via a YAML file with top-level keys:
      - sample_count (int)
      - confidence_interval (float, 0..1)
      - turnout_rate (float, 0..1)
      - constituencies (list of constituency dicts)

    Each constituency must contain: name, seats, polling_stations,
    registered_voters and parties. Each party must contain name and sample_count.
    """

    def __init__(self, config_path: str) -> None:
        """Load configuration and initialise internal state.

        Reads the YAML configuration from `config_path`, parses the top-level
        parameters (sample_count, confidence_interval, turnout_rate) and the
        list of constituencies. Performs validation via helper methods.
        Constituencies with empty sample counts will be marked for skipping.

        Args:
            config_path: Path to the YAML configuration file.
        """
        cfg = self._load_config(config_path)
        self._original_cfg = cfg  # keep original for fields we don't fully control
        self.sample_count = int(cfg['sample_count'])
        self.conf_lvl = float(cfg['confidence_interval'])
        self.turnout = float(cfg['turnout_rate'])
        self.constituencies = cfg['constituencies']
        self.skip_constituencies = set()  # Track constituencies to skip
        # Monotonic counter to record arrival order when samples first become complete
        self.update_counter = int(cfg.get('update_counter', 0) or 0)

        self._validate_top_level()
        self._validate_constituencies()

        # After validation, update arrival order markers for newly completed constituencies
        self._update_arrival_order()

        # results will be populated by `analyse()`
        self.results = []
        self.predicted_next_government = "Inconclusive"
        self.popular_vote = {}

    # -----------------
    # Configuration
    # -----------------
    def _load_config(self, path: str) -> Dict[str, Any]:
        """Read and parse a YAML configuration file.

        Returns the parsed YAML as a dictionary (wrapper for yaml.safe_load).
        The config may include previously computed results in the 'analysis'
        field of each constituency.
        """
        with open(path, 'r') as fh:
            return yaml.safe_load(fh)
        
    def _save_config(self, path: str) -> None:
        """Save the current configuration and analysis results back to YAML.
        
        Updates the constituencies with their computed statistics (confidence
        intervals, spreads, winner status) and writes back to the config file.
        Maintains original YAML structure with CIs under parties and winner/spread
        at constituency level.
        """
        # Build base of new config preserving some unrelated keys if present
        now_epoch = int(time.time())
        cfg = {
            'sample_count': self.sample_count,
            'confidence_interval': float(self.conf_lvl),
            'turnout_rate': float(self.turnout),
            'popular_vote': self.popular_vote,
            'predicted_next_government': self.predicted_next_government,
            'update_counter': int(self.update_counter),
            # Store last update as epoch seconds
            'last_updated': now_epoch,
            'constituencies': []
        }

        # Match results with constituencies and merge
        for c in self.constituencies:
            const_result = next((r for r in self.results if r['Constituency'] == c['name']), None)
            merged = {
                'name': c['name'],
                'seats': c['seats'],
                'polling_stations': c['polling_stations'],
                'registered_voters': c['registered_voters']
            }

            if const_result:
                # Add winner (use 'Inconclusive' if not called) and spread at constituency level
                merged['winner'] = (
                    str(const_result['Winner Candidate']) if const_result['Called'] else 'Inconclusive'
                )
                merged['spread'] = float(const_result.get('MaxSpread', 0.0))

                # Update parties with CIs alongside name and sample_count
                merged['parties'] = []
                for p in c['parties']:
                    party_result = next((pr for pr in const_result['Parties'] if pr['name'] == p['name']), None)
                    party_data = {
                        'name': p['name'],
                        'sample_count': p['sample_count']
                    }
                    if party_result:
                        ci_tuple = party_result['ci']
                        party_data['confidence_interval'] = [float(ci_tuple[0]), float(ci_tuple[1])]
                    merged['parties'].append(party_data)
            else:
                # If no analysis results, keep original party data
                merged['parties'] = [{'name': p['name'], 'sample_count': p['sample_count']} for p in c['parties']]

            # Preserve/update arrival sequence marker if present on constituency dict
            if 'update_seq' in c:
                merged['update_seq'] = int(c['update_seq']) if c['update_seq'] is not None else None

            cfg['constituencies'].append(merged)

        with open(path, 'w') as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)

    # -----------------
    # National metrics
    # -----------------

    def _compute_popular_vote_all(self) -> Dict[str, float]:
        """Compute national popular vote proportions for all parties.

        Weights by expected actual voters (registered_voters * turnout_rate).
        Constituencies without usable samples contribute their full weight to
        a special 'Pending Data' bucket.
        Returns a mapping party -> proportion (0..1). Will include 'Pending Data'
        when applicable. Also stores result in self.popular_vote.
        """
        weights_by_party: Dict[str, float] = {}
        total_weight = 0.0

        for c in self.constituencies:
            weight = float(c.get('registered_voters', 0)) * float(self.turnout)
            if weight <= 0:
                continue
            total_weight += weight

            if c['name'] in self.skip_constituencies:
                weights_by_party['Pending Data'] = weights_by_party.get('Pending Data', 0.0) + weight
                continue

            parties = c.get('parties', [])
            if not parties:
                weights_by_party['Pending Data'] = weights_by_party.get('Pending Data', 0.0) + weight
                continue
            total_samples = sum((p.get('sample_count') or 0) for p in parties)
            if total_samples <= 0:
                weights_by_party['Pending Data'] = weights_by_party.get('Pending Data', 0.0) + weight
                continue

            for p in parties:
                name = str(p.get('name'))
                sc = int(p.get('sample_count') or 0)
                prop = (sc / total_samples) if total_samples > 0 else 0.0
                if prop <= 0:
                    continue
                weights_by_party[name] = weights_by_party.get(name, 0.0) + (prop * weight)

        if total_weight <= 0:
            self.popular_vote = {}
            return self.popular_vote

        self.popular_vote = {k: (v / total_weight) for k, v in weights_by_party.items()}
        return self.popular_vote

    def _compute_predicted_government(self) -> str:
        """Determine the predicted next government from called seats.

        A party forms the next government if its called seats exceed half of
        the total contested seats. If no such party, returns 'Inconclusive'.
        """
        total_seats = sum(int(c.get('seats', 0)) for c in self.constituencies)
        if total_seats <= 0:
            self.predicted_next_government = "Inconclusive"
            return self.predicted_next_government

        tally: Dict[str, int] = {}
        for r in self.results:
            if r.get('Called'):
                party = str(r.get('Winner Candidate'))
                tally[party] = tally.get(party, 0) + int(r.get('Seats', 0))

        majority_threshold = total_seats / 2.0
        # Find any party exceeding the threshold
        winner_party = None
        winner_seats = 0
        for party, seats in tally.items():
            if seats > majority_threshold and seats > winner_seats:
                winner_party = party
                winner_seats = seats

        self.predicted_next_government = winner_party if winner_party else "Inconclusive"
        return self.predicted_next_government

    def _validate_top_level(self) -> None:
        """Validate top-level scalar configuration values.

        Ensures sample_count is positive and that confidence_interval and
        turnout_rate are within [0, 1].
        """
        if self.sample_count <= 0:
            raise ValueError(f"sample_count must be a positive integer, got {self.sample_count}")
        if not (0.0 <= self.conf_lvl <= 1.0):
            raise ValueError(f"confidence_interval must be between 0 and 1 (inclusive), got {self.conf_lvl}")
        if not (0.0 <= self.turnout <= 1.0):
            raise ValueError(f"turnout_rate must be between 0 and 1 (inclusive), got {self.turnout}")

    def _validate_constituencies(self) -> None:
        """Validate all constituencies and their parties.

        Coerces numeric fields to int and checks sensible ranges.
        Marks constituencies with empty sample counts as invalid for analysis.
        """
        required_const = ['name', 'seats', 'polling_stations', 'registered_voters', 'parties']
        required_party = ['name', 'sample_count']

        if not isinstance(self.constituencies, list) or len(self.constituencies) == 0:
            raise ValueError("config must contain a non-empty 'constituencies' list")

        # Keep track of which constituencies to skip (have empty sample counts)
        self.skip_constituencies = set()

        for c in self.constituencies:
            _require_keys(c, required_const, 'constituency')
            c['seats'] = int(c['seats'])
            c['polling_stations'] = int(c['polling_stations'])
            c['registered_voters'] = int(c['registered_voters'])

            if c['seats'] <= 0:
                raise ValueError(f"seats must be positive in constituency {c['name']}")
            if c['polling_stations'] <= 0:
                raise ValueError(f"polling_stations must be positive in constituency {c['name']}")
            if c['registered_voters'] < 0:
                raise ValueError(f"registered_voters must be non-negative in constituency {c['name']}")

            plist = c['parties']
            if not isinstance(plist, list) or len(plist) == 0:
                raise ValueError(f"Constituency '{c['name']}' must have at least one party")

            # Check for empty sample counts
            empty_counts = False
            for p in plist:
                _require_keys(p, required_party, f"party in constituency {c['name']}")
                if p['sample_count'] is None:  # Empty/null sample count
                    empty_counts = True
                    break
                try:
                    p['sample_count'] = int(p['sample_count'])
                    if p['sample_count'] < 0:
                        raise ValueError(f"party sample_count must be a non-negative integer for party {p.get('name')} in constituency {c['name']}")
                except (ValueError, TypeError):
                    empty_counts = True
                    break
            
            if empty_counts:
                print(f"Note: Skipping constituency '{c['name']}' - incomplete sample count data")
                self.skip_constituencies.add(c['name'])
                c['has_complete_samples'] = False
            else:
                c['has_complete_samples'] = True

    def _update_arrival_order(self) -> None:
        """Assign/update monotonically increasing arrival order for newly completed constituencies.

        For any constituency that now has complete samples (has_complete_samples True)
        but no 'update_seq' marker yet, assign the next sequence number from
        self.update_counter. This enables recency ordering and top-5 tracking.
        """
        # load any previous per-constituency markers from original config
        prev_seq_by_name: Dict[str, int] = {}
        try:
            for c in self._original_cfg.get('constituencies', []):
                if 'update_seq' in c and c['update_seq'] is not None:
                    prev_seq_by_name[c['name']] = int(c['update_seq'])
        except Exception:
            prev_seq_by_name = {}

        for c in self.constituencies:
            # Preserve prior sequence if present
            if c.get('name') in prev_seq_by_name:
                c['update_seq'] = prev_seq_by_name[c['name']]
                continue
            # Assign new sequence only when samples just became complete
            if c.get('has_complete_samples') and c.get('update_seq') is None:
                self.update_counter += 1
                c['update_seq'] = self.update_counter
            else:
                # Leave as None for pending data
                c.setdefault('update_seq', None)

    # -----------------
    # Statistical helpers
    # -----------------
    def _compute_confint(self, p: float, N: int, h: int) -> Tuple[float, float]:
        """Return a (low, high) confidence interval for proportion p.

        The estimator used is the overall proportion of votes for a party within
        a constituency, based on per-party sample counts collected across
        polling stations (treated as strata). This method returns a Normal
        approximation interval centered at the observed proportion `p` with
        variance derived from a stratified simple random sampling model.

        Derivation notes (assumptions and steps):

        - We treat the constituency as a finite population of size N_eff =
          round(N * turnout), i.e. the expected number of actual voters.

        - The constituency is partitioned into H strata (polling stations).
          We assume strata are approximately equal in size (same number of
          voters per polling station). Denote the number of strata by H and
          the (assumed) number of sampled observations per stratum by nh.

        - Let h be the party-specific sample count used in this code for the
          party of interest (we pass max(1, sample_count) to avoid division by
          zero in practice). The estimator for the party's proportion is the
          weighted average of stratum-level sample proportions. Under the
          equal-stratum-size assumption and equal allocation across strata the
          variance of that estimator simplifies.

        - Applying the finite-population correction (FPC) for each stratum
          and summing under the equal-size/equal-allocation assumptions gives
          the variance approximation used here:

            variance ≈ ((N_eff - h * nh) * p * (1 - p)) / ((N_eff - h) * h * nh)

          Intuition for terms:
            - p(1-p) is the binomial variance for proportions.
            - nh and h appear in the denominator because more samples per
              stratum (nh) and more total party samples (h) reduce variance.
            - (N_eff - h * nh) / (N_eff - h) is the finite-population
              correction factor aggregated across strata; it reduces variance
              when the sample fraction is non-negligible.

        - The Normal approximation (Central Limit Theorem) is then used to
          produce approximate (1 - alpha) confidence intervals around p.

        Important caveats:
          - The formula assumes approximate equality of stratum sizes and a
            reasonably large number of sampled observations so the Normal
            approximation is sensible. For very small h or when p is near 0
            or 1 the function returns degenerate or clamped intervals.
          - This is an engineering approximation intended for quick, simple
            election-calling logic and not a full survey-sampling analysis.

        Returns degenerate intervals for boundary cases and clamps results to
        [0,1].
        """
        # boundary cases
        if p <= 0.0:
            return (0.0, 0.0)
        if p >= 1.0:
            return (1.0, 1.0)

        nh = self.sample_count
        N_eff = round(N * self.turnout)

        # safety: avoid division by zero; if parameters don't permit variance calc, return point estimate
        if h <= 0 or nh <= 0 or (N_eff - h) <= 0 or (N_eff - h * nh) <= 0:
            return (p, p)

        variance = ((N_eff - h * nh) * p * (1 - p)) / ((N_eff - h) * h * nh)
        sigma = math.sqrt(max(0.0, variance))
        ci = st.norm.interval(confidence=self.conf_lvl, loc=p, scale=sigma)
        low, high = max(0.0, ci[0]), min(1.0, ci[1])
        return (low, high)

    # -----------------
    # Analysis
    # -----------------
    def _party_cis_for_constituency(self, c: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Compute per-party CI information for a single constituency.

        For each party this function computes:
          - the sample proportion (sample_count / sum(sample_count))
          - a confidence interval computed by `_compute_confint`
          - the interval spread (upper - lower)

        The function also enforces the requirement that the per-party
        `sample_count` values sum to 100 for the constituency (this project
        uses 100 sample allocation as a convention). If that validation is
        undesirable it can be relaxed to accept arbitrary totals and
        normalise proportions.

        Args:
            c: Constituency dictionary from the configuration.

        Returns:
            A list of dictionaries, each containing keys: name, sample_count,
            prop, ci, spread.
        """
        plist = c['parties']
        total_samples = sum(p['sample_count'] for p in plist)
        if total_samples != 100:
            raise ValueError(f"Total sample_count for all parties in constituency {c['name']} must sum to 100, got {total_samples}")

        result: List[Dict[str, Any]] = []
        for p in plist:
            sc = p['sample_count']
            prop = (sc / total_samples) if total_samples > 0 else 0.0
            ci = self._compute_confint(prop, c['registered_voters'], c['polling_stations'])
            spread = ci[1] - ci[0]
            result.append({'name': p.get('name'), 'sample_count': sc, 'prop': prop, 'ci': ci, 'spread': spread})
        return result

    def analyse(self) -> None:
        """Run the core analysis across all constituencies.

        For each constituency the function:
          - Skips constituencies with empty sample counts
          - If the constituency has cached analysis results and no sample_counts
            changed, reuse those results
          - Otherwise computes per-party CIs via `_party_cis_for_constituency`
          - Determines the current leader by sample_count
          - Marks the constituency as "called" if the leader's lower CI bound
            exceeds the maximum upper bound of all other parties (non-overlap rule)
          - Records the largest per-party CI spread for the constituency

        Results are stored in `self.results` and the human-readable summary is
        printed by `_print_seat_summary`. Results are also saved back to the
        config file for future runs.
        """
        rows: List[Dict[str, Any]] = []

        for c in self.constituencies:
            # Skip constituencies with empty sample counts
            if c['name'] in self.skip_constituencies:
                continue
            # Check if we can use cached results
            if 'analysis' in c:
                cached = c['analysis']
                # Verify sample counts haven't changed
                cached_counts = {p.get('name'): p.get('sample_count') for p in cached.get('party_results', [])}
                current_counts = {p.get('name'): p.get('sample_count') for p in c.get('parties', [])}
                if cached_counts == current_counts:
                    # Use cached results
                    rows.append({
                        'Constituency': c['name'],
                        'Seats': c['seats'],
                        'Winner Candidate': cached['winner'],
                        'Called': cached['called'],
                        'Parties': [{
                            'name': p['name'],
                            'sample_count': p['sample_count'],
                            'prop': p.get('proportion', 0),
                            'ci': p.get('confidence_interval', (0, 0)),
                            'spread': p.get('spread', 0)
                        } for p in cached.get('party_results', [])],
                        'MaxSpread': cached.get('max_spread', 0),
                        'MaxSpreadParty': cached.get('max_spread_party', '')
                    })
                    continue
            
            # Compute new results
            party_cis = self._party_cis_for_constituency(c)

            if not party_cis:
                rows.append({'Constituency': c['name'], 'Seats': c['seats'], 'Winner Candidate': 'Undecided',
                             'Called': False, 'Parties': [], 'MaxSpread': 0.0, 'MaxSpreadParty': ''})
                continue

            # leading party by sample_count
            sorted_by_sample = sorted(party_cis, key=lambda x: (x['sample_count'], x['prop']), reverse=True)
            leader = sorted_by_sample[0]
            other_uppers = [p['ci'][1] for p in party_cis if p['name'] != leader['name']]
            max_other_upper = max(other_uppers) if other_uppers else -1.0
            called = leader['ci'][0] > max_other_upper
            winner_name = leader['name'] if called else 'Undecided'

            max_p = max(party_cis, key=lambda x: x['spread'])
            rows.append({
                'Constituency': c['name'],
                'Seats': c['seats'],
                'Winner Candidate': leader['name'],
                'Called': called,
                'Parties': party_cis,
                'MaxSpread': max_p['spread'],
                'MaxSpreadParty': max_p['name']
            })

        # Finalize results and national metrics
        self.results = rows
        self._compute_popular_vote_all()
        self._compute_predicted_government()
        #self._print_seat_summary()

    # -----------------
    # Output
    # -----------------
    def _print_seat_summary(self) -> None:
        """Print a human-friendly summary of results stored in `self.results`.

        The summary lists each constituency with its called/undecided status,
        per-party sample counts and CIs, and a final seat tally sorted by
        descending number of seats. It also reports the single largest CI
        spread observed across all constituencies.
        """
        seats: Dict[str, int] = {}
        for r in self.results:
            winner_label = r['Winner Candidate'] if r['Called'] else 'Undecided'
            seats[winner_label] = seats.get(winner_label, 0) + r['Seats']

        print("Election Results Summary:")
        print(f"Confidence: {int(self.conf_lvl * 100)}%")

        national_max_spread = 0.0
        national_max_entry: Tuple[str, str] = ("", "")

        for r in self.results:
            status = f"CALLED: {r['Winner Candidate']}" if r['Called'] else f"Undecided (leading: {r['Winner Candidate']})"
            print(f"{r['Constituency']} - {status}")
            for p in r['Parties']:
                print(f"  {p['name']}: samples={p['sample_count']}, prop={p['prop']:.3f}, CI=({p['ci'][0]:.3f}, {p['ci'][1]:.3f}), spread={p['spread']:.4f}")
            print('')

            if r.get('MaxSpread', 0) > national_max_spread:
                national_max_spread = r.get('MaxSpread', 0)
                national_max_entry = (r['Constituency'], r.get('MaxSpreadParty'))

        print('Seat tally (sorted):')
        for party, count in sorted(seats.items(), key=lambda x: x[1], reverse=True):
            print(f"  {party}: {count}")

        if national_max_entry[0]:
            print(f"\nLargest per-constituency CI spread: {national_max_spread:.4f} in {national_max_entry[0]} (party: {national_max_entry[1]})")
        else:
            print("\nNo spread data available.")


if __name__ == "__main__":
    cfg = 'config.yml'
    election_caller = ElectionCaller(cfg)
    election_caller.analyse()
    election_caller._save_config(cfg)  # Save results back to YAML

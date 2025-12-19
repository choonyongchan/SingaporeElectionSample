import pandas as pd

# URL of the website
url = "https://yourstudent-gemini.fandom.com/wiki/Polling_districts"

# Read HTML tables from the URL
tables = pd.read_html(url)

if len(tables) == 0:
    print("No tables found on the page")
else:
    df = tables[0]
    # Try to detect constituency and polling station column names (case-insensitive)
    cols = [c for c in df.columns]
    constituency_col = next((c for c in cols if 'constitu' in c.lower()), None)
    station_col = next((c for c in cols if 'poll' in c.lower() or 'station' in c.lower()), None)

    if constituency_col is None or station_col is None:
        print("Could not automatically detect constituency or polling station columns.")
        print("Columns found:", cols)
    else:
        # Count unique polling stations per constituency
        counts = (
            df.dropna(subset=[constituency_col, station_col])
              .groupby(constituency_col)[station_col]
              .nunique()
              .reset_index(name='unique_polling_stations')
              .sort_values(by=constituency_col, key=lambda s: s.str.lower(), ascending=True)
        )

        # Total unique polling stations across the whole dataset
        total_unique_overall = df.dropna(subset=[station_col])[station_col].nunique()

        # (Optional) Sum of per-constituency unique counts (may double-count stations appearing in multiple constituencies)
        total_sum_by_constituency = counts['unique_polling_stations'].sum()

        print(counts.to_string(index=False))
        print()
        print(f"Total unique polling stations (overall): {total_unique_overall}")
        print(f"Sum of per-constituency unique counts: {total_sum_by_constituency}")

import pandas as pd
import scipy.stats as st
import yaml

class ElectionCaller:
    """
    A class to analyze and predict election results based on voter data.
    Handles confidence interval calculations and election outcome predictions.
    """

    def __init__(self, filepath: str, turnout: float, confidence_leveL: float, voter_per_station: int, sample_count: int) -> 'ElectionCaller':
        """
        Initialize ElectionCaller with path to voter data file.
        
        Args:
            filepath (str): Path to CSV file containing election data
        """
        if not filepath.endswith('.csv'): raise ValueError("File must be a CSV.")
        if not (0 < turnout < 1): raise ValueError("Turnout must be between 0 and 1.")
        if not (0 < confidence_leveL < 1): raise ValueError("Confidence level must be between 0 and 1.")
        if not (voter_per_station > 0): raise ValueError("Voter per station must be greater than 0.")

        data: pd.DataFrame = pd.read_csv(filepath)
        data = data.sort_values(by='Constituency', ignore_index=True)
        data['Voter Turnout'] = turnout * data['Registered Voters']

        self.data: pd.DataFrame = data
        self.conf_lvl: float = confidence_leveL
        self.voter_per_station: int = voter_per_station
        self.sample_count: int = sample_count
    
    def get_confint(self, p: float, N: int) -> tuple[float, float]:
        """
        Calculate confidence interval using stratified sampling.
        
        Args:
            p (float): Proportion of votes
            N (int): Total number of voters
        Returns:
            tuple[float, float]: Lower and upper bounds of confidence interval
        """
        if p == 0 or p == 1:
            return (p, p)

        sigma: float = (
            (self.voter_per_station / self.sample_count) * 
            (p * (1 - p)) / N
        ) ** 0.5 #((p*(1-p)) / (100*(N/2600)))**0.5
        ci: tuple[float, float] = st.norm.interval(
            confidence=self.conf_lvl,
            loc=p,
            scale=sigma
        )
        return (float(round(ci[0], 2)), float(round(ci[1], 2)))

    def predict(self, ci_pap: tuple[float, float], ci_bestopp: tuple[float, float]) -> str:
        """
        Determine election outcome based on confidence intervals.
        
        Args:
            ci_pap: Confidence interval for PAP
            ci_bestopp: Confidence interval for opposition
        Returns:
            str: Election outcome ("PAP Wins", "BestOpp Wins", or "Undecided")
        """
        if ci_pap[0] > ci_bestopp[-1]:
            return "PAP"
        elif ci_bestopp[0] > ci_pap[-1]:
            return "Opposition"
        return "Undecided"

    def analyse(self) -> None:
        """Execute the complete election analysis workflow."""
        
        # Calculate confidence intervals
        self.data['Confidence Interval PAP'] = self.data.apply(
            lambda row: self.get_confint(row['PAP'] / 100, row['Voter Turnout']),
            axis=1
        )
        self.data['Confidence Interval Best Opposition'] = self.data.apply(
            lambda row: self.get_confint(row['BestOpp'] / 100, row['Voter Turnout']),
            axis=1
        )
        
        # Predict results and format output
        self.data['Predicted Result'] = self.data.apply(
            lambda row: self.predict(row['Confidence Interval PAP'], row['Confidence Interval Best Opposition']),
            axis=1
        )
        results = self.data[['Constituency', 'Predicted Result', 'Confidence Interval PAP', 'Confidence Interval Best Opposition']]
        
        # Print results
        self._export_seat_summary(results)
    
    def _export_seat_summary(self, results: pd.DataFrame) -> None:
        """Print summary of seat distribution."""
        seats = {
            'PAP': self.data[self.data['Predicted Result'] == 'PAP']['Seats'].sum(),
            'Opposition': self.data[self.data['Predicted Result'] == 'Opposition']['Seats'].sum(),
            'Undecided': self.data[self.data['Predicted Result'] == 'Undecided']['Seats'].sum()
        }

        print("Election Results Summary:")

        output = []
        output.append(f"# Election Results Summary\n")
        output.append(f"Confidence Level: {int(self.conf_lvl * 100)}%\n")
        output.append("[![Run main.py](https://github.com/choonyongchan/SingaporeElectionSample/actions/workflows/run.yml/badge.svg)](https://github.com/choonyongchan/SingaporeElectionSample/actions/workflows/run.yml)\n")
        output.append("## Detailed Results\n")
        output.append(results.to_markdown(index=False))
        output.append("\n## Seat Distribution\n")
        for party, count in seats.items():
            output.append(f"- {party}: {count} seats\n")
        
        # Write to README.md
        with open('README.md', 'w') as f:
            f.writelines(output)

        

if __name__ == "__main__":
    
    # Read configuration from XML
    try:
        # Read configuration from YAML
        with open('config.yaml', 'r') as file:
            config = yaml.safe_load(file)
        
        # Extract configuration values
        filepath: str = str(config.get('filepath'))
        turnout: float = float(config.get('turnout'))
        confidence: float = float(config.get('confidence'))
        voter_per_station: int = int(config.get('voter_per_station'))
        sample_count: int = int(config.get('sample_count'))

        if not all((filepath, turnout, confidence)):
            raise ValueError("Invalid configuration values.")
        
        # Initialize and run analysis
        election_caller = ElectionCaller(filepath, turnout, confidence, voter_per_station, sample_count)
        election_caller.analyse()
        
    except FileNotFoundError:
        print("Error: config.xml not found. Using default values.")
        election_caller = ElectionCaller('data.csv')
        election_caller.analyse()

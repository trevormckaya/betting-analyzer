import asyncio
import aiohttp
import json
import time
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from enum import Enum
import math

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class BetType(Enum):
    MONEYLINE = "h2h"  # Head to head (moneyline)
    SPREAD = "spreads"
    TOTAL = "totals"
    PLAYER_PROPS = "player_props"

@dataclass
class OddsData:
    """Represents odds data from a sportsbook"""
    bookmaker: str
    sport: str
    sport_title: str
    event_name: str
    commence_time: str
    market_type: str
    selection: str
    odds: float  # American odds format
    timestamp: datetime

@dataclass
class EVOpportunity:
    """Represents a positive expected value betting opportunity"""
    sport_title: str
    event_name: str
    commence_time: str
    market_type: str
    selection: str
    target_book: str  # The sportsbook to place the bet with
    target_odds: float  # Odds from the target book
    reference_odds: float  # Best odds used for consensus probability
    ev_percentage: float
    recommended_bet_size: float
    target_implied_prob: float
    consensus_prob: float  # The consensus "true" probability
    timestamp: datetime

class OddsConverter:
    """Utility class for converting between different odds formats"""
    
    @staticmethod
    def american_to_decimal(american_odds: float) -> float:
        """Convert American odds to decimal format"""
        if american_odds > 0:
            return (american_odds / 100) + 1
        else:
            return (100 / abs(american_odds)) + 1
    
    @staticmethod
    def decimal_to_american(decimal_odds: float) -> float:
        """Convert decimal odds to American format"""
        if decimal_odds >= 2:
            return (decimal_odds - 1) * 100
        else:
            return -100 / (decimal_odds - 1)
    
    @staticmethod
    def american_to_implied_probability(american_odds: float) -> float:
        """Convert American odds to implied probability"""
        if american_odds > 0:
            return 100 / (american_odds + 100)
        else:
            return abs(american_odds) / (abs(american_odds) + 100)

class KellyCalculator:
    """Kelly criterion calculator for bet sizing"""
    
    @staticmethod
    def half_kelly(bankroll: float, odds: float, true_prob: float) -> float:
        """
        Calculate half Kelly bet size
        
        Args:
            bankroll: Total available bankroll
            odds: American odds being offered
            true_prob: True probability of the outcome (from Pinnacle)
        
        Returns:
            Recommended bet size using half Kelly criterion
        """
        if true_prob <= 0 or true_prob >= 1:
            return 0
        
        decimal_odds = OddsConverter.american_to_decimal(odds)
        
        # Kelly formula: f = (bp - q) / b
        # where b = decimal odds - 1, p = true probability, q = 1 - p
        b = decimal_odds - 1
        p = true_prob
        q = 1 - p
        
        kelly_fraction = (b * p - q) / b
        
        # Use half Kelly for risk management
        half_kelly_fraction = kelly_fraction / 2
        
        # Ensure we don't bet more than bankroll or negative amounts
        if half_kelly_fraction <= 0:
            return 0
        
        return min(bankroll * half_kelly_fraction, bankroll * 0.05)  # Max 5% of bankroll per bet

class TheOddsAPIClient:
    """Client for The Odds API"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.the-odds-api.com/v4"
        self.session = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={'User-Agent': 'Mozilla/5.0 (compatible; EVBettingBot/1.0)'}
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def get_sports(self) -> List[dict]:
        """Get list of available sports"""
        try:
            url = f"{self.base_url}/sports"
            params = {'apiKey': self.api_key}
            
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    logger.error(f"Sports API error: {response.status}")
                    return []
                
                data = await response.json()
                logger.info(f"Fetched {len(data)} available sports")
                return data
        
        except Exception as e:
            logger.error(f"Error fetching sports: {e}")
            return []
    
    async def get_odds_for_sport(self, sport_key: str, markets: List[str] = None, include_props: bool = True) -> List[OddsData]:
        """Get odds for a specific sport including player props"""
        if markets is None:
            if include_props:
                markets = ['h2h', 'spreads', 'totals']  # We'll get player props separately
            else:
                markets = ['h2h', 'spreads', 'totals']
        
        all_odds = []
        
        try:
            # Get main markets (h2h, spreads, totals)
            url = f"{self.base_url}/sports/{sport_key}/odds"
            params = {
                'apiKey': self.api_key,
                'regions': 'us',
                'markets': ','.join(markets),
                'oddsFormat': 'american',
                'dateFormat': 'iso'
            }
            
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    logger.error(f"Odds API error for {sport_key}: {response.status}")
                    return []
                
                data = await response.json()
                logger.info(f"Fetched main market odds for {len(data)} events in {sport_key}")
                all_odds.extend(self._parse_odds_data(data, sport_key))
            
            # Get player props if requested
            if include_props:
                props_url = f"{self.base_url}/sports/{sport_key}/events"
                async with self.session.get(props_url, params={'apiKey': self.api_key}) as events_response:
                    if events_response.status == 200:
                        events_data = await events_response.json()
                        
                        # Get player props for each event
                        for event in events_data[:5]:  # Limit to first 5 events to conserve API calls
                            event_id = event.get('id')
                            if event_id:
                                props_odds = await self._get_player_props(sport_key, event_id)
                                all_odds.extend(props_odds)
        
        except Exception as e:
            logger.error(f"Error fetching odds for {sport_key}: {e}")
            return []
        
        return all_odds
    
    async def _get_player_props(self, sport_key: str, event_id: str) -> List[OddsData]:
        """Get player props for a specific event"""
        try:
            url = f"{self.base_url}/sports/{sport_key}/events/{event_id}/odds"
            params = {
                'apiKey': self.api_key,
                'regions': 'us',
                'markets': 'player_points,player_rebounds,player_assists,player_threes,player_pass_tds,player_rush_yds,player_receiving_yds',
                'oddsFormat': 'american',
                'dateFormat': 'iso'
            }
            
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                if isinstance(data, list) and len(data) > 0:
                    return self._parse_odds_data(data, sport_key, is_props=True)
                
        except Exception as e:
            logger.error(f"Error fetching player props for event {event_id}: {e}")
        
        return []
    
    def _parse_odds_data(self, data: List[dict], sport_key: str, is_props: bool = False) -> List[OddsData]:
        """Parse The Odds API response into OddsData objects"""
        odds_list = []
        current_time = datetime.now(timezone.utc)
        
        try:
            for event in data:
                sport_title = event.get('sport_title', sport_key)
                event_name = f"{event.get('home_team', 'Unknown')} vs {event.get('away_team', 'Unknown')}"
                commence_time_str = event.get('commence_time', '')
                
                # Filter for games within the next week
                if commence_time_str:
                    try:
                        commence_time = datetime.fromisoformat(commence_time_str.replace('Z', '+00:00'))
                        time_diff = commence_time - current_time
                        
                        # Skip games more than 7 days away or games that already started
                        if time_diff.total_seconds() < 0 or time_diff.days > 7:
                            continue
                    except:
                        continue  # Skip if we can't parse the time
                
                for bookmaker in event.get('bookmakers', []):
                    bookmaker_name = bookmaker.get('title', bookmaker.get('key', 'Unknown'))
                    
                    for market in bookmaker.get('markets', []):
                        market_type = market.get('key', 'h2h')
                        
                        # Add prop indicator to market type
                        if is_props and not market_type.startswith('player_'):
                            continue
                        
                        for outcome in market.get('outcomes', []):
                            selection = outcome.get('name', 'Unknown')
                            odds = outcome.get('price', 0)
                            
                            # Add point/line information for props and spreads
                            point = outcome.get('point')
                            if point is not None:
                                selection += f" ({point:+g})"
                            
                            if isinstance(odds, (int, float)) and odds != 0:
                                odds_data = OddsData(
                                    bookmaker=bookmaker_name,
                                    sport=sport_key,
                                    sport_title=sport_title,
                                    event_name=event_name,
                                    commence_time=commence_time_str,
                                    market_type=market_type,
                                    selection=selection,
                                    odds=float(odds),
                                    timestamp=current_time
                                )
                                odds_list.append(odds_data)
        
        except Exception as e:
            logger.error(f"Error parsing odds data: {e}")
        
        return odds_list

class EVCalculator:
    """Calculator for identifying positive expected value opportunities"""
    
    def __init__(self, bankroll: float = 1000.0):
        self.bankroll = bankroll
        self.kelly_calc = KellyCalculator()
    
    def find_ev_opportunities(self, all_odds: List[OddsData]) -> List[EVOpportunity]:
        """Find +EV opportunities by comparing DraftKings against other sportsbooks"""
        opportunities = []
        
        # Group odds by event, market, and selection
        odds_groups = self._group_odds(all_odds)
        
        for key, odds_group in odds_groups.items():
            if len(odds_group) < 2:
                continue  # Need at least 2 bookmakers to compare
            
            # Find DraftKings odds in this group
            dk_odds = [odd for odd in odds_group if 'draftkings' in odd.bookmaker.lower()]
            other_odds = [odd for odd in odds_group if 'draftkings' not in odd.bookmaker.lower()]
            
            if not dk_odds or not other_odds:
                continue  # Need both DraftKings and other books
            
            # Use DraftKings odds as our target
            dk_odd = dk_odds[0]  # Take first DraftKings entry
            
            # Find opportunities by comparing DraftKings against other books
            opportunities.extend(self._find_dk_opportunities(dk_odd, other_odds))
        
        return sorted(opportunities, key=lambda x: x.ev_percentage, reverse=True)
    
    def _find_dk_opportunities(self, dk_odd: OddsData, other_odds: List[OddsData]) -> List[EVOpportunity]:
        """Find +EV opportunities for DraftKings bets using other books as consensus"""
        opportunities = []
        
        if not other_odds:
            return opportunities
        
        # Calculate implied probabilities for all other books
        other_probs = []
        for odd in other_odds:
            implied_prob = OddsConverter.american_to_implied_probability(odd.odds)
            other_probs.append((odd, implied_prob))
        
        # Sort by implied probability (lowest = best odds for bettor = most accurate assessment)
        other_probs.sort(key=lambda x: x[1])
        
        # Use average of best 2-3 books as consensus "true" probability
        top_books = other_probs[:min(3, len(other_probs))]
        consensus_prob = sum(prob for _, prob in top_books) / len(top_books)
        
        # Get best reference odd for display
        best_reference_odd = other_probs[0][0] if other_probs else None
        
        # Calculate EV for DraftKings bet using consensus probability
        ev_opp = self._calculate_ev_consensus(dk_odd, consensus_prob, best_reference_odd)
        if ev_opp and ev_opp.ev_percentage > 0:
            opportunities.append(ev_opp)
        
        return opportunities
    
    def _group_odds(self, odds_list: List[OddsData]) -> Dict[str, List[OddsData]]:
        """Group odds by event, market type, and selection"""
        groups = {}
        
        for odd in odds_list:
            # Create a unique key for each betting opportunity
            key = f"{odd.sport}_{odd.event_name}_{odd.market_type}_{odd.selection}"
            
            if key not in groups:
                groups[key] = []
            
            groups[key].append(odd)
        
        return groups
    
    def _calculate_ev_consensus(self, target_odd: OddsData, true_prob: float, reference_odd: OddsData) -> Optional[EVOpportunity]:
        """Calculate expected value using a given true probability"""
        try:
            target_prob = OddsConverter.american_to_implied_probability(target_odd.odds)
            
            # Calculate expected value
            target_decimal = OddsConverter.american_to_decimal(target_odd.odds)
            ev = (true_prob * target_decimal) - 1
            ev_percentage = ev * 100
            
            # Only consider positive EV opportunities with minimum threshold
            if ev_percentage <= 2.0:  # Minimum 2.0% EV (increased threshold)
                return None
            
            # Calculate recommended bet size using half Kelly
            bet_size = self.kelly_calc.half_kelly(
                bankroll=self.bankroll,
                odds=target_odd.odds,
                true_prob=true_prob
            )
            
            # Only recommend bets of at least $1
            if bet_size < 1.0:
                return None
            
            return EVOpportunity(
                sport_title=target_odd.sport_title,
                event_name=target_odd.event_name,
                commence_time=target_odd.commence_time,
                market_type=target_odd.market_type,
                selection=target_odd.selection,
                target_book=target_odd.bookmaker,  # Show which book to bet with
                target_odds=target_odd.odds,
                reference_odds=reference_odd.odds,
                ev_percentage=ev_percentage,
                recommended_bet_size=bet_size,
                target_implied_prob=target_prob,
                consensus_prob=true_prob,
                timestamp=datetime.now(timezone.utc)
            )
        
        except Exception as e:
            logger.error(f"Error calculating EV consensus: {e}")
            return None
    
    def _calculate_ev(self, other_odd: OddsData, pinnacle_odd: OddsData) -> Optional[EVOpportunity]:
        """Calculate expected value for a betting opportunity"""
        try:
            # Use Pinnacle's implied probability as the "true" probability
            pinnacle_prob = OddsConverter.american_to_implied_probability(pinnacle_odd.odds)
            other_prob = OddsConverter.american_to_implied_probability(other_odd.odds)
            
            # Calculate expected value
            other_decimal = OddsConverter.american_to_decimal(other_odd.odds)
            ev = (pinnacle_prob * other_decimal) - 1
            ev_percentage = ev * 100
            
            # Only consider positive EV opportunities with minimum threshold
            if ev_percentage <= 0.5:  # Minimum 0.5% EV (lowered threshold)
                return None
            
            # Calculate recommended bet size using half Kelly
            bet_size = self.kelly_calc.half_kelly(
                bankroll=self.bankroll,
                odds=other_odd.odds,
                true_prob=pinnacle_prob
            )
            
            # Only recommend bets of at least $1
            if bet_size < 1.0:  # Lowered from $5 to $1
                return None
            
            return EVOpportunity(
                sport_title=other_odd.sport_title,
                event_name=other_odd.event_name,
                commence_time=other_odd.commence_time,
                market_type=other_odd.market_type,
                selection=other_odd.selection,
                dk_odds=other_odd.odds,
                pinnacle_odds=pinnacle_odd.odds,
                ev_percentage=ev_percentage,
                recommended_bet_size=bet_size,
                dk_implied_prob=other_prob,
                pinnacle_implied_prob=pinnacle_prob,
                timestamp=datetime.now(timezone.utc)
            )
        
        except Exception as e:
            logger.error(f"Error calculating EV: {e}")
            return None

class BettingArbitrageService:
    """Main service for finding +EV betting opportunities"""
    
    def __init__(self, api_key: str, bankroll: float = 1000.0):
        self.api_key = api_key
        self.bankroll = bankroll
        self.ev_calculator = EVCalculator(bankroll)
        self.api_client = None
    
    async def get_available_sports(self) -> List[dict]:
        """Get list of available sports from The Odds API"""
        async with TheOddsAPIClient(self.api_key) as client:
            return await client.get_sports()
    
    async def scan_sport_for_opportunities(self, sport_key: str, markets: List[str] = None, debug: bool = False, include_props: bool = True) -> List[EVOpportunity]:
        """Scan a specific sport for +EV opportunities"""
        if markets is None:
            markets = ['h2h', 'spreads', 'totals']  # Include all main markets
        
        logger.info(f"Scanning {sport_key} for DraftKings +EV opportunities (games within next 7 days)...")
        
        async with TheOddsAPIClient(self.api_key) as client:
            # Fetch all odds for this sport including props
            all_odds = await client.get_odds_for_sport(sport_key, markets, include_props)
            
            if not all_odds:
                logger.warning(f"No odds data found for {sport_key}")
                return []
            
            # Filter to only games within next week (this is now done in parsing)
            logger.info(f"Found {len(all_odds)} total odds entries within next 7 days")
            
            if debug:
                self._debug_odds_analysis(all_odds)
            
            # Find EV opportunities
            opportunities = self.ev_calculator.find_ev_opportunities(all_odds)
            
            logger.info(f"Found {len(opportunities)} DraftKings +EV opportunities in {sport_key}")
            return opportunities
    
    def _debug_odds_analysis(self, all_odds: List[OddsData]):
        """Debug function to analyze odds data and potential issues"""
        print(f"\n=== DEBUG: DraftKings-Focused Analysis ===")
        print(f"Total odds entries: {len(all_odds)}")
        
        # Count by bookmaker
        bookmaker_counts = {}
        for odd in all_odds:
            bookmaker_counts[odd.bookmaker] = bookmaker_counts.get(odd.bookmaker, 0) + 1
        
        print(f"Bookmakers found: {list(bookmaker_counts.keys())}")
        for book, count in bookmaker_counts.items():
            print(f"  {book}: {count} odds")
        
        # Check for DraftKings specifically
        dk_count = sum(1 for odd in all_odds if 'draftkings' in odd.bookmaker.lower())
        print(f"DraftKings odds available: {dk_count > 0} ({dk_count} odds)")
        
        # Show sample odds comparisons focusing on DraftKings
        odds_groups = self.ev_calculator._group_odds(all_odds)
        dk_groups = 0
        
        for key, odds_group in list(odds_groups.items())[:5]:  # First 5 groups
            dk_odds = [odd for odd in odds_group if 'draftkings' in odd.bookmaker.lower()]
            other_odds = [odd for odd in odds_group if 'draftkings' not in odd.bookmaker.lower()]
            
            if dk_odds and other_odds:
                dk_groups += 1
                print(f"\nSample DraftKings comparison for: {key}")
                
                dk_odd = dk_odds[0]
                dk_prob = OddsConverter.american_to_implied_probability(dk_odd.odds)
                print(f"  🎯 DraftKings: {dk_odd.odds:+.0f} (Implied: {dk_prob:.1%})")
                
                print(f"  Other books:")
                for other_odd in other_odds[:3]:  # Show up to 3 other books
                    other_prob = OddsConverter.american_to_implied_probability(other_odd.odds)
                    print(f"    {other_odd.bookmaker}: {other_odd.odds:+.0f} (Implied: {other_prob:.1%})")
        
        print(f"Groups with DraftKings + other books: {dk_groups}")
        print("=== END DEBUG ===\n")
    
    async def scan_multiple_sports(self, sport_keys: List[str], markets: List[str] = None) -> List[EVOpportunity]:
        """Scan multiple sports for +EV opportunities"""
        all_opportunities = []
        
        for sport_key in sport_keys:
            opportunities = await self.scan_sport_for_opportunities(sport_key, markets)
            all_opportunities.extend(opportunities)
            
            # Add a small delay to respect API rate limits
            await asyncio.sleep(0.5)
        
        return sorted(all_opportunities, key=lambda x: x.ev_percentage, reverse=True)
    
    def print_opportunities(self, opportunities: List[EVOpportunity], output_file: str = None):
        """Print formatted output of +EV opportunities to file or terminal"""
        if not opportunities:
            message = "No +EV opportunities found."
            if output_file:
                with open(output_file, 'w') as f:
                    f.write(message + "\n")
                print(f"Results saved to {output_file}")
            else:
                print(message)
            return
        
        # Create the formatted output
        output_lines = []
        output_lines.append("=" * 100)
        output_lines.append("POSITIVE EXPECTED VALUE OPPORTUNITIES")
        output_lines.append("Method: Multi-Book Comparison (Consensus vs Individual Books)")
        output_lines.append(f"Bankroll: ${self.bankroll:,.2f}")
        output_lines.append(f"Total Opportunities: {len(opportunities)}")
        output_lines.append(f"Analysis Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        output_lines.append("=" * 100)
        
        total_recommended_bets = sum(opp.recommended_bet_size for opp in opportunities)
        
        for i, opp in enumerate(opportunities, 1):
            # Parse commence time
            try:
                commence_dt = datetime.fromisoformat(opp.commence_time.replace('Z', '+00:00'))
                time_str = commence_dt.strftime('%m/%d %H:%M UTC')
            except:
                time_str = opp.commence_time
            
            output_lines.append(f"\n{i}. {opp.sport_title}")
            output_lines.append(f"   Event: {opp.event_name}")
            output_lines.append(f"   Time: {time_str}")
            output_lines.append(f"   Market: {opp.market_type.upper()} - {opp.selection}")
            output_lines.append(f"   🎯 BET WITH: {opp.target_book}")
            output_lines.append(f"   Target Odds: {opp.target_odds:+.0f} (Implied: {opp.target_implied_prob:.1%})")
            output_lines.append(f"   Consensus Prob: {opp.consensus_prob:.1%}")
            output_lines.append(f"   Expected Value: +{opp.ev_percentage:.2f}%")
            output_lines.append(f"   Recommended Bet: ${opp.recommended_bet_size:.2f}")
            output_lines.append(f"   Potential Profit: ${opp.recommended_bet_size * opp.ev_percentage / 100:.2f}")
        
        output_lines.append(f"\n" + "=" * 100)
        output_lines.append("SUMMARY:")
        output_lines.append(f"Total Recommended Bets: ${total_recommended_bets:.2f}")
        output_lines.append(f"Percentage of Bankroll: {total_recommended_bets/self.bankroll:.1%}")
        
        if opportunities:
            avg_ev = sum(opp.ev_percentage for opp in opportunities) / len(opportunities)
            output_lines.append(f"Average EV: +{avg_ev:.2f}%")
        
        output_lines.append("=" * 100)
        output_lines.append("Note: 'Consensus Prob' is derived from the most favorable odds across all books")
        output_lines.append("This represents a market-based estimate of true probability")
        
        # Also show breakdown by sport
        if opportunities:
            sport_breakdown = {}
            for opp in opportunities:
                sport = opp.sport_title
                if sport not in sport_breakdown:
                    sport_breakdown[sport] = 0
                sport_breakdown[sport] += 1
            
            output_lines.append(f"\nBreakdown by Sport:")
            for sport, count in sport_breakdown.items():
                output_lines.append(f"  {sport}: {count} opportunities")
        
        # Write to file or print to terminal
        if output_file:
            with open(output_file, 'w') as f:
                for line in output_lines:
                    f.write(line + "\n")
            print(f"Results saved to {output_file}")
            print(f"Total opportunities found: {len(opportunities)}")
            print(f"Total recommended betting amount: ${total_recommended_bets:.2f}")
        else:
            for line in output_lines:
                print(line)

# Popular sports mapping for easy reference
POPULAR_SPORTS = {
    'NFL': 'americanfootball_nfl',
    'NBA': 'basketball_nba', 
    'MLB': 'baseball_mlb',
    'NHL': 'icehockey_nhl',
    'NCAA Football': 'americanfootball_ncaaf',
    'NCAA Basketball': 'basketball_ncaab',
    'EPL': 'soccer_epl',
    'Champions League': 'soccer_uefa_champs_league',
    'MMA': 'mma_mixed_martial_arts',
    'Tennis': 'tennis_atp'
}

async def main():
    """Main execution function"""
    # Your API key
    API_KEY = "c60ee6532a93ad39fcc5b67a00f81da6"
    
    # Initialize the service with updated parameters
    service = BettingArbitrageService(api_key=API_KEY, bankroll=200.0)  # $200 bankroll
    
    try:
        print("Getting available sports...")
        sports = await service.get_available_sports()
        
        # Show available sports
        print(f"\nAvailable Sports ({len(sports)}):")
        active_sports = []
        for sport in sports[:15]:  # Show first 15 sports
            if sport.get('has_outrights', False) or sport.get('active', True):
                print(f"  - {sport.get('title', 'Unknown')}: {sport.get('key', 'Unknown')}")
                active_sports.append(sport.get('key'))
        
        # Scan popular sports for DraftKings opportunities
        sports_to_scan = [
            'americanfootball_nfl',
            'basketball_nba', 
            'americanfootball_ncaaf'  # Focus on 3 main sports for testing
        ]
        
        # Filter to only scan sports that are actually available
        available_sports_to_scan = [sport for sport in sports_to_scan if sport in active_sports]
        
        if not available_sports_to_scan:
            print("\nNo active sports found to scan. Trying first few available sports...")
            available_sports_to_scan = active_sports[:3]  # Scan first 3 available sports
        
        print(f"\nScanning sports for DraftKings +EV opportunities: {available_sports_to_scan}")
        print("Including: Game lines (moneyline, spread, totals) + Player props")
        print("Time filter: Games within next 7 days only")
        
        # Scan for opportunities across multiple markets including props
        opportunities = await service.scan_multiple_sports(available_sports_to_scan)
        
        # Print results to file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_filename = f"ev_opportunities_{timestamp}.txt"
        service.print_opportunities(opportunities, output_filename)
        
        # If no opportunities found, run debug mode on first sport
        if not opportunities and available_sports_to_scan:
            print(f"\nNo +EV opportunities found. Running debug analysis on {available_sports_to_scan[0]}...")
            debug_opportunities = await service.scan_sport_for_opportunities(available_sports_to_scan[0], debug=True)
            
            # Also try with even lower thresholds for demonstration
            print(f"\nTrying with ultra-low thresholds (0.1% EV minimum)...")
            service.ev_calculator = EVCalculator(service.bankroll)  # Reset calculator
            # Temporarily lower threshold even more for demo
            demo_opportunities = []
            async with TheOddsAPIClient(service.api_key) as client:
                all_odds = await client.get_odds_for_sport(available_sports_to_scan[0])
                if all_odds:
                    odds_groups = service.ev_calculator._group_odds(all_odds)
                    for key, odds_group in list(odds_groups.items())[:5]:  # Check first 5
                        if len(odds_group) >= 2:
                            group_opps = service.ev_calculator._find_group_opportunities(odds_group)
                            demo_opportunities.extend(group_opps)
            
            if demo_opportunities:
                print(f"\nFound {len(demo_opportunities)} potential opportunities with relaxed criteria:")
                for opp in demo_opportunities[:3]:  # Show top 3
                    print(f"  {opp.event_name} - {opp.selection}: +{opp.ev_percentage:.3f}% EV")
        
        # Print results
        service.print_opportunities(opportunities)
        
        # Save to file if opportunities found
        if opportunities:
            filename = f"ev_opportunities_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            # Convert opportunities to dict format for JSON serialization
            opportunities_dict = []
            for opp in opportunities:
                opp_dict = {
                    'sport_title': opp.sport_title,
                    'event_name': opp.event_name,
                    'commence_time': opp.commence_time,
                    'market_type': opp.market_type,
                    'selection': opp.selection,
                    'target_book': opp.target_book,
                    'target_odds': opp.target_odds,
                    'reference_odds': opp.reference_odds,
                    'ev_percentage': opp.ev_percentage,
                    'recommended_bet_size': opp.recommended_bet_size,
                    'target_implied_prob': opp.target_implied_prob,
                    'consensus_prob': opp.consensus_prob,
                    'timestamp': opp.timestamp.isoformat()
                }
                opportunities_dict.append(opp_dict)
                
            with open(filename, 'w') as f:
                json.dump(opportunities_dict, f, indent=2)
            print(f"\nResults saved to {filename}")
        
        # Show API usage info
        print(f"\nAPI calls used in this scan: ~{len(available_sports_to_scan) + 1}")
        print("Note: The Odds API has usage limits. Check your usage at https://the-odds-api.com/account")
    
    except Exception as e:
        logger.error(f"Error in main execution: {e}")
        print(f"Error: {e}")

# Utility function to run debug scans with file output
async def debug_scan(sport_key: str, bankroll: float = 1000.0, save_to_file: bool = True):
    """Debug scan to see all odds comparisons for a single sport"""
    API_KEY = "c60ee6532a93ad39fcc5b67a00f81da6"
    service = BettingArbitrageService(api_key=API_KEY, bankroll=bankroll)
    
    opportunities = await service.scan_sport_for_opportunities(sport_key, debug=True)
    
    if save_to_file:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"ev_debug_{sport_key}_{timestamp}.txt"
        service.print_opportunities(opportunities, filename)
    else:
        service.print_opportunities(opportunities)
    
    return opportunities

if __name__ == "__main__":
    print("Starting DraftKings +EV Betting Service...")
    print("Using The Odds API for real-time data")
    print("Comparing DraftKings to other sportsbooks for +EV identification")
    print("Focus: Games within next 7 days, 2%+ EV, $200 bankroll")
    print("-" * 60)
    
    # Run the main service
    asyncio.run(main())

# Example of how to run a quick scan for just NFL
# asyncio.run(quick_scan('americanfootball_nfl', 2000.0))
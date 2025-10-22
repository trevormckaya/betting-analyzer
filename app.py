from flask import Flask, render_template, jsonify, request
import asyncio
import json
import statistics
from datetime import datetime, timezone
import logging
from typing import List, Dict, Any, Optional
import os
# At the top of app.py, add:
from bet_tracker import bet_tracker_bp, integrate_with_app

# After creating your Flask app, add:
#app.register_blueprint(bet_tracker_bp)
# Import your existing betting analysis classes
import sys
sys.path.append('.')

from betting_analyzer import (
    BettingArbitrageService, 
    EVOpportunity, 
    OddsData,
    TheOddsAPIClient,
    EVCalculator,
    OddsConverter
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'

# Global variables for caching
cached_opportunities = []
cached_raw_data = []
last_update_time = None
is_loading = False

# Configuration
API_KEY = "c60ee6532a93ad39fcc5b67a00f81da6"

# Sports configuration with their supported markets
SPORTS_CONFIG = {
    'americanfootball_nfl': {
        'name': 'NFL',
        'standard_markets': ['h2h', 'spreads', 'totals'],
        'props': True
    },
    'basketball_nba': {
        'name': 'NBA',
        'standard_markets': ['h2h', 'spreads', 'totals'],
        'props': True
    },
    'americanfootball_ncaaf': {
        'name': 'NCAA Football',
        'standard_markets': ['h2h', 'spreads', 'totals'],
        'props': True
    },
    'baseball_mlb': {
        'name': 'MLB',
        'standard_markets': ['h2h', 'spreads', 'totals'],
        'props': True
    }
}

# Target books we want to find +EV opportunities for
TARGET_BOOKS = ['BetMGM', 'DraftKings', 'FanDuel', 'Caesars']

class EnhancedEVCalculator:
    """Enhanced EV Calculator supporting player props and multiple sports"""
    
    def __init__(self):
        self.kelly_calc = EVCalculator(100).kelly_calc
    
    def find_ev_opportunities(self, all_odds: List[OddsData]) -> List[Dict]:
        """Find +EV opportunities including player props, grouped by event"""
        # First, find all individual opportunities
        individual_opportunities = self._find_individual_opportunities(all_odds)
        
        logger.info(f"Found {len(individual_opportunities)} individual +EV opportunities")
        
        # Group opportunities by event
        if individual_opportunities:
            grouped_opportunities = self._group_by_event(individual_opportunities)
            logger.info(f"Grouped into {len(grouped_opportunities)} events")
            return grouped_opportunities
        else:
            logger.info("No opportunities found to group")
            return []
    
    def _find_individual_opportunities(self, all_odds: List[OddsData]) -> List[Dict]:
        """Find all individual +EV opportunities"""
        individual_opportunities = []
        
        # Group odds by event, market, and selection
        odds_groups = self._group_odds(all_odds)
        
        logger.info(f"Processing {len(odds_groups)} odds groups")
        
        for key, odds_group in odds_groups.items():
            if len(odds_group) < 3:  # Need at least 3 books total
                continue
            
            # Separate target books from consensus books
            target_odds = [odd for odd in odds_group if any(target in odd.bookmaker for target in TARGET_BOOKS)]
            consensus_odds = [odd for odd in odds_group if not any(target in odd.bookmaker for target in TARGET_BOOKS)]
            
            if not target_odds or len(consensus_odds) < 2:
                continue
            
            # Calculate median consensus probability
            consensus_probs = [OddsConverter.american_to_implied_probability(odd.odds) for odd in consensus_odds]
            median_prob = statistics.median(consensus_probs)
            
            # Find best consensus odd for display
            consensus_with_probs = [(odd, OddsConverter.american_to_implied_probability(odd.odds)) for odd in consensus_odds]
            best_consensus_odd = min(consensus_with_probs, key=lambda x: abs(x[1] - median_prob))[0]
            
            # Check each target book for +EV
            for target_odd in target_odds:
                ev_opp = self._calculate_ev(target_odd, median_prob, best_consensus_odd)
                if ev_opp and ev_opp['ev_percentage'] > 2.0:  # 2% minimum EV
                    individual_opportunities.append(ev_opp)
        
        return individual_opportunities
    
    def _group_by_event(self, opportunities: List[Dict]) -> List[Dict]:
        """Group opportunities by event, separating standard markets from props"""
        event_groups = {}
        
        logger.info(f"Grouping {len(opportunities)} individual opportunities by event")
        
        for opp in opportunities:
            # Create event key using sport, teams, and time
            event_key = f"{opp['sport_title']}_{opp['event_name']}_{opp['commence_time']}"
            
            if event_key not in event_groups:
                event_groups[event_key] = {
                    'sport_title': opp['sport_title'],
                    'sport_key': opp['sport_key'],
                    'event_name': opp['event_name'],
                    'commence_time': opp['commence_time'],
                    'standard_markets': [],
                    'player_props': [],
                    'max_ev': 0,
                    'total_profit_100': 0,
                    'total_profit_500': 0,
                    'total_opportunities': 0
                }
            
            # Categorize as standard market or player prop
            if self._is_player_prop(opp['market_type']):
                event_groups[event_key]['player_props'].append(opp)
            else:
                event_groups[event_key]['standard_markets'].append(opp)
            
            # Update aggregates
            event_groups[event_key]['max_ev'] = max(event_groups[event_key]['max_ev'], opp['ev_percentage'])
            event_groups[event_key]['total_profit_100'] += opp['profit_100']
            event_groups[event_key]['total_profit_500'] += opp['profit_500']
            event_groups[event_key]['total_opportunities'] += 1
        
        logger.info(f"Created {len(event_groups)} event groups")
        
        # Sort opportunities within each event by EV percentage
        for event_key, event_data in event_groups.items():
            event_data['standard_markets'].sort(key=lambda x: x['ev_percentage'], reverse=True)
            event_data['player_props'].sort(key=lambda x: x['ev_percentage'], reverse=True)
            
            logger.info(f"Event {event_key}: {len(event_data['standard_markets'])} standard, "
                       f"{len(event_data['player_props'])} props")
        
        # Convert to list and sort events by highest EV opportunity
        grouped_events = list(event_groups.values())
        grouped_events.sort(key=lambda x: x['max_ev'], reverse=True)
        
        logger.info(f"Returning {len(grouped_events)} grouped events")
        return grouped_events
    
    def _is_player_prop(self, market_type: str) -> bool:
        """Check if market type is a player prop"""
        prop_keywords = ['player_', 'batter_', 'pitcher_']
        return any(keyword in market_type.lower() for keyword in prop_keywords)
    
    def _group_odds(self, odds_list: List[OddsData]) -> Dict[str, List[OddsData]]:
        """Group odds by event, market type, and selection"""
        groups = {}
        for odd in odds_list:
            key = f"{odd.sport}_{odd.event_name}_{odd.market_type}_{odd.selection}"
            if key not in groups:
                groups[key] = []
            groups[key].append(odd)
        return groups
    
    def _calculate_ev(self, target_odd: OddsData, true_prob: float, reference_odd: OddsData) -> Optional[Dict]:
        """Calculate expected value and bet sizes"""
        try:
            target_prob = OddsConverter.american_to_implied_probability(target_odd.odds)
            target_decimal = OddsConverter.american_to_decimal(target_odd.odds)
            
            # Calculate EV
            ev = (true_prob * target_decimal) - 1
            ev_percentage = ev * 100
            
            if ev_percentage <= 2.0:
                return None
            
            # Calculate bet sizes for $100 and $500 bankrolls
            bet_100 = self.kelly_calc.half_kelly(100, target_odd.odds, true_prob)
            bet_500 = self.kelly_calc.half_kelly(500, target_odd.odds, true_prob)
            
            # Minimum bet sizes
            if bet_100 < 1.0:
                return None
            
            # Parse commence time
            try:
                commence_dt = datetime.fromisoformat(target_odd.commence_time.replace('Z', '+00:00'))
                time_str = commence_dt.strftime('%m/%d %H:%M UTC')
            except:
                time_str = target_odd.commence_time
            
            # Format market type for display
            market_display = self._format_market_type(target_odd.market_type)
            
            return {
                'sport_title': target_odd.sport_title,
                'sport_key': target_odd.sport,
                'event_name': target_odd.event_name,
                'commence_time': time_str,
                'market_type': target_odd.market_type,
                'market_display': market_display,
                'selection': target_odd.selection,
                'target_book': target_odd.bookmaker,
                'target_odds': target_odd.odds,
                'reference_odds': reference_odd.odds,
                'ev_percentage': round(ev_percentage, 2),
                'target_implied_prob': round(target_prob * 100, 1),
                'consensus_prob': round(true_prob * 100, 1),
                'bet_100': round(bet_100, 2),
                'bet_500': round(bet_500, 2),
                'profit_100': round(bet_100 * ev_percentage / 100, 2),
                'profit_500': round(bet_500 * ev_percentage / 100, 2)
            }
        
        except Exception as e:
            logger.error(f"Error calculating EV: {e}")
            return None
    
    def _format_market_type(self, market_type: str) -> str:
        """Format market type for better display"""
        market_map = {
            'h2h': 'Moneyline',
            'spreads': 'Spread',
            'totals': 'Total',
            'player_points': 'Player Points',
            'player_rebounds': 'Player Rebounds',
            'player_assists': 'Player Assists',
            'player_threes': 'Player 3-Pointers',
            'player_pass_tds': 'Player Pass TDs',
            'player_rush_yds': 'Player Rush Yards',
            'player_receiving_yds': 'Player Receiving Yards',
            'batter_home_runs': 'Batter Home Runs',
            'batter_hits': 'Batter Hits',
            'batter_total_bases': 'Batter Total Bases',
            'batter_rbis': 'Batter RBIs',
            'batter_runs_scored': 'Batter Runs',
            'batter_stolen_bases': 'Batter Stolen Bases',
            'pitcher_strikeouts': 'Pitcher Strikeouts',
            'pitcher_hits_allowed': 'Pitcher Hits Allowed',
            'pitcher_walks': 'Pitcher Walks',
            'pitcher_earned_runs': 'Pitcher Earned Runs'
        }
        return market_map.get(market_type, market_type.replace('_', ' ').title())

def run_async_analysis():
    """Run the betting analysis asynchronously"""
    global cached_opportunities, cached_raw_data, last_update_time, is_loading
    
    try:
        is_loading = True
        logger.info("Starting analysis...")
        
        # Create new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Get raw odds data for all configured sports
        async def get_all_raw_odds():
            all_raw_odds = []
            async with TheOddsAPIClient(API_KEY) as client:
                for sport_key, sport_config in SPORTS_CONFIG.items():
                    logger.info(f"Fetching odds for {sport_config['name']}...")
                    
                    # Fetch standard markets and props
                    sport_odds = await client.get_odds_for_sport(
                        sport_key, 
                        sport_config['standard_markets'], 
                        include_props=sport_config['props']
                    )
                    all_raw_odds.extend(sport_odds)
                    
                    logger.info(f"Fetched {len(sport_odds)} odds for {sport_config['name']}")
            
            return all_raw_odds
        
        # Fetch data synchronously in this thread
        raw_odds = loop.run_until_complete(get_all_raw_odds())
        loop.close()
        
        cached_raw_data = raw_odds
        
        logger.info(f"Fetched {len(raw_odds)} total odds entries across all sports")
        
        # Count available books and sports
        available_books = set(odd.bookmaker for odd in raw_odds)
        sports_found = set(odd.sport_title for odd in raw_odds)
        target_books_found = [book for book in TARGET_BOOKS if any(target in avail_book for avail_book in available_books for target in [book])]
        other_books_found = [book for book in available_books if not any(target in book for target in TARGET_BOOKS)]
        
        logger.info(f"Sports with odds: {sports_found}")
        logger.info(f"Target books found: {target_books_found}")
        logger.info(f"Consensus books available: {len(other_books_found)}")
        
        # Run EV analysis
        ev_calculator = EnhancedEVCalculator()
        opportunities = ev_calculator.find_ev_opportunities(raw_odds)
        
        cached_opportunities = opportunities
        last_update_time = datetime.now()
        
        logger.info(f"Analysis complete. Found {len(opportunities)} events with +EV opportunities")
        
        # Log breakdown by sport
        sport_breakdown = {}
        for event in opportunities:
            sport = event['sport_title']
            sport_breakdown[sport] = sport_breakdown.get(sport, 0) + event['total_opportunities']
        
        logger.info(f"Breakdown by sport: {sport_breakdown}")
        
        return opportunities
        
    except Exception as e:
        logger.error(f"Error in analysis: {e}", exc_info=True)
        cached_opportunities = []
        return []
    finally:
        is_loading = False
        logger.info("Analysis thread finished")

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/api/opportunities')
def get_opportunities():
    """API endpoint to get cached opportunities"""
    global cached_opportunities, last_update_time
    
    # Ensure we always return valid data
    if cached_opportunities is None:
        cached_opportunities = []
    
    # Calculate total individual opportunities from grouped events
    total_count = 0
    if cached_opportunities and isinstance(cached_opportunities, list):
        total_count = sum(event.get('total_opportunities', 0) for event in cached_opportunities)
    
    return jsonify({
        'opportunities': cached_opportunities,
        'last_update': last_update_time.isoformat() if last_update_time else None,
        'total_count': total_count,
        'is_loading': is_loading,
        'target_books': TARGET_BOOKS,
        'sports_config': {k: v['name'] for k, v in SPORTS_CONFIG.items()}
    })

@app.route('/api/refresh', methods=['POST'])
def refresh_data():
    """API endpoint to trigger data refresh"""
    global is_loading
    
    if is_loading:
        return jsonify({'error': 'Analysis already in progress'}), 429
    
    try:
        # Run analysis in background
        import threading
        thread = threading.Thread(target=run_async_analysis)
        thread.daemon = True
        thread.start()
        
        return jsonify({'message': 'Analysis started', 'is_loading': True})
    except Exception as e:
        logger.error(f"Error starting refresh: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug')
def debug_analysis():
    """Debug endpoint to check analysis state"""
    global cached_opportunities, cached_raw_data
    
    debug_info = {
        'cached_opportunities_count': len(cached_opportunities) if cached_opportunities else 0,
        'cached_opportunities_type': type(cached_opportunities).__name__,
        'cached_raw_data_count': len(cached_raw_data) if cached_raw_data else 0,
        'is_loading': is_loading,
        'target_books': TARGET_BOOKS,
        'sports_config': SPORTS_CONFIG
    }
    
    # Sample some data if available
    if cached_opportunities:
        debug_info['sample_opportunity'] = cached_opportunities[0] if len(cached_opportunities) > 0 else None
    
    if cached_raw_data:
        available_books = set(odd.bookmaker for odd in cached_raw_data)
        target_books_found = [book for book in available_books if any(target in book for target in TARGET_BOOKS)]
        sports_found = set(odd.sport_title for odd in cached_raw_data)
        
        # Count props vs standard markets
        props_count = sum(1 for odd in cached_raw_data if 'player_' in odd.market_type.lower() or 'batter_' in odd.market_type.lower() or 'pitcher_' in odd.market_type.lower())
        standard_count = len(cached_raw_data) - props_count
        
        debug_info['available_books'] = sorted(list(available_books))
        debug_info['target_books_found'] = target_books_found
        debug_info['sports_found'] = sorted(list(sports_found))
        debug_info['props_count'] = props_count
        debug_info['standard_markets_count'] = standard_count
    
    return jsonify(debug_info)

# Create templates directory and HTML template
def create_templates():
    """Create the HTML template file"""
    templates_dir = 'templates'
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)
    
    # Copy the simplified HTML (will be updated in next artifact)
    # This is handled by the separate index.html artifact

if __name__ == '__main__':
    # Create templates directory
    create_templates()
    
    print("🚀 Starting Enhanced +EV Betting Web Application...")
    print("📊 Features:")
    print(f"  - Sports: {', '.join([v['name'] for v in SPORTS_CONFIG.values()])}")
    print("  - Markets: Moneyline, Spread, Totals + Player Props")
    print("  - Target books: BetMGM, DraftKings, FanDuel, Caesars")
    print("  - Consensus: Median of all other available books")
    print("  - Bet sizing: $100 and $500 bankroll examples")
    print("  - Minimum 2% EV threshold")
    print("\n🌐 Access the app at: http://localhost:5000")
    print("📝 Note: Make sure betting_analyzer.py is in the same directory")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
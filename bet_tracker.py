"""
Bet Tracker Module - Backend for tracking betting outcomes and analyzing performance
Provides persistent storage, CRUD operations, and analytics for betting data.
"""

from flask import Blueprint, jsonify, request, render_template
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import json
import os
import logging
from dataclasses import dataclass, asdict
from enum import Enum
import sqlite3
from contextlib import contextmanager
import random  # For demo data generation

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create Blueprint for bet tracker routes
bet_tracker_bp = Blueprint('bet_tracker', __name__, url_prefix='/tracker')

class BetResult(Enum):
    """Enum for bet result states"""
    PENDING = "pending"
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"

@dataclass
class BetRecord:
    """
    Data class representing a single bet record.
    
    Attributes:
        id: Unique identifier for the bet
        date_placed: Timestamp when bet was placed
        sport: Sport category (e.g., NFL, NBA)
        event_name: Description of the event (e.g., "Lakers vs Warriors")
        market_type: Type of bet (moneyline, spread, total)
        selection: What was bet on (team name, over/under)
        bookmaker: Where the bet was placed
        odds: American odds format
        stake: Amount wagered
        ev_percentage: Expected value percentage
        implied_prob: Implied probability from odds
        consensus_prob: Market consensus probability
        result: Outcome of the bet
        payout: Amount won/lost
        notes: Optional notes about the bet
        created_at: Database record creation time
        updated_at: Last update time
    """
    id: Optional[int] = None
    date_placed: str = ""
    sport: str = ""
    event_name: str = ""
    market_type: str = ""
    selection: str = ""
    bookmaker: str = ""
    odds: float = 0.0
    stake: float = 0.0
    ev_percentage: float = 0.0
    implied_prob: float = 0.0
    consensus_prob: float = 0.0
    result: str = BetResult.PENDING.value
    payout: float = 0.0
    notes: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class BetDatabase:
    """
    SQLite database handler for bet records.
    Provides CRUD operations and analytics queries.
    """
    
    def __init__(self, db_path: str = "bets.db"):
        """Initialize database connection and create tables if needed"""
        self.db_path = db_path
        self._create_tables()
    
    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Enable column access by name
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            conn.close()
    
    def _create_tables(self):
        """Create database tables if they don't exist"""
        with self.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_placed DATETIME NOT NULL,
                    sport TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    selection TEXT NOT NULL,
                    bookmaker TEXT NOT NULL,
                    odds REAL NOT NULL,
                    stake REAL NOT NULL,
                    ev_percentage REAL NOT NULL,
                    implied_prob REAL NOT NULL,
                    consensus_prob REAL NOT NULL,
                    result TEXT DEFAULT 'pending',
                    payout REAL DEFAULT 0,
                    notes TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create index for faster queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_date_placed 
                ON bets(date_placed)
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_result 
                ON bets(result)
            """)
    
    def add_bet(self, bet: BetRecord) -> int:
        """
        Add a new bet record to the database.
        
        Args:
            bet: BetRecord object to add
            
        Returns:
            ID of the newly created record
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO bets (
                    date_placed, sport, event_name, market_type, selection,
                    bookmaker, odds, stake, ev_percentage, implied_prob,
                    consensus_prob, result, payout, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                bet.date_placed, bet.sport, bet.event_name, bet.market_type,
                bet.selection, bet.bookmaker, bet.odds, bet.stake,
                bet.ev_percentage, bet.implied_prob, bet.consensus_prob,
                bet.result, bet.payout, bet.notes
            ))
            return cursor.lastrowid
    
    def update_bet(self, bet_id: int, updates: Dict[str, Any]) -> bool:
        """
        Update an existing bet record.
        
        Args:
            bet_id: ID of the bet to update
            updates: Dictionary of fields to update
            
        Returns:
            True if successful, False otherwise
        """
        # Build dynamic UPDATE query
        allowed_fields = [
            'result', 'payout', 'notes', 'stake', 'odds', 
            'ev_percentage', 'implied_prob', 'consensus_prob'
        ]
        
        fields_to_update = []
        values = []
        
        for field, value in updates.items():
            if field in allowed_fields:
                fields_to_update.append(f"{field} = ?")
                values.append(value)
        
        if not fields_to_update:
            return False
        
        # Add updated_at timestamp
        fields_to_update.append("updated_at = CURRENT_TIMESTAMP")
        values.append(bet_id)
        
        query = f"""
            UPDATE bets 
            SET {', '.join(fields_to_update)}
            WHERE id = ?
        """
        
        with self.get_connection() as conn:
            cursor = conn.execute(query, values)
            return cursor.rowcount > 0
    
    def delete_bet(self, bet_id: int) -> bool:
        """
        Delete a bet record from the database.
        
        Args:
            bet_id: ID of the bet to delete
            
        Returns:
            True if successful, False otherwise
        """
        with self.get_connection() as conn:
            cursor = conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
            return cursor.rowcount > 0
    
    def get_bet(self, bet_id: int) -> Optional[Dict]:
        """
        Retrieve a single bet by ID.
        
        Args:
            bet_id: ID of the bet to retrieve
            
        Returns:
            Dictionary representation of the bet, or None if not found
        """
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_bets(self, 
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     result_filter: Optional[str] = None,
                     limit: Optional[int] = None) -> List[Dict]:
        """
        Retrieve all bets with optional filters.
        
        Args:
            start_date: Filter bets after this date (ISO format)
            end_date: Filter bets before this date (ISO format)
            result_filter: Filter by result status
            limit: Maximum number of records to return
            
        Returns:
            List of bet dictionaries
        """
        query = "SELECT * FROM bets WHERE 1=1"
        params = []
        
        if start_date:
            query += " AND date_placed >= ?"
            params.append(start_date)
        
        if end_date:
            query += " AND date_placed <= ?"
            params.append(end_date)
        
        if result_filter and result_filter != 'all':
            query += " AND result = ?"
            params.append(result_filter)
        
        query += " ORDER BY date_placed DESC"
        
        if limit:
            query += f" LIMIT {limit}"
        
        with self.get_connection() as conn:
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Calculate comprehensive betting statistics.
        
        Returns:
            Dictionary containing various performance metrics
        """
        with self.get_connection() as conn:
            # Overall statistics
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as total_bets,
                    COUNT(CASE WHEN result = 'win' THEN 1 END) as wins,
                    COUNT(CASE WHEN result = 'loss' THEN 1 END) as losses,
                    COUNT(CASE WHEN result = 'push' THEN 1 END) as pushes,
                    COUNT(CASE WHEN result = 'pending' THEN 1 END) as pending,
                    SUM(stake) as total_staked,
                    SUM(CASE WHEN result = 'win' THEN payout ELSE 0 END) as total_winnings,
                    SUM(CASE WHEN result = 'loss' THEN -stake ELSE 0 END) as total_losses,
                    AVG(ev_percentage) as avg_ev,
                    AVG(stake) as avg_stake
                FROM bets
            """)
            overall_stats = dict(cursor.fetchone())
            
            # Calculate profit/loss
            total_profit = (overall_stats['total_winnings'] or 0) + (overall_stats['total_losses'] or 0)
            
            # Win rate (excluding pending and pushes)
            resolved_bets = overall_stats['wins'] + overall_stats['losses']
            win_rate = (overall_stats['wins'] / resolved_bets * 100) if resolved_bets > 0 else 0
            
            # ROI calculation
            roi = (total_profit / overall_stats['total_staked'] * 100) if overall_stats['total_staked'] else 0
            
            # Performance by sport
            cursor = conn.execute("""
                SELECT 
                    sport,
                    COUNT(*) as bets,
                    COUNT(CASE WHEN result = 'win' THEN 1 END) as wins,
                    SUM(CASE WHEN result = 'win' THEN payout WHEN result = 'loss' THEN -stake ELSE 0 END) as profit
                FROM bets
                WHERE result IN ('win', 'loss')
                GROUP BY sport
            """)
            sport_performance = [dict(row) for row in cursor.fetchall()]
            
            # Recent form (last 20 resolved bets)
            cursor = conn.execute("""
                SELECT result, stake, payout
                FROM bets
                WHERE result IN ('win', 'loss')
                ORDER BY date_placed DESC
                LIMIT 20
            """)
            recent_form = [dict(row) for row in cursor.fetchall()]
            
            return {
                'overall': {
                    'total_bets': overall_stats['total_bets'],
                    'wins': overall_stats['wins'],
                    'losses': overall_stats['losses'],
                    'pushes': overall_stats['pushes'],
                    'pending': overall_stats['pending'],
                    'win_rate': round(win_rate, 2),
                    'total_staked': round(overall_stats['total_staked'] or 0, 2),
                    'total_profit': round(total_profit, 2),
                    'roi': round(roi, 2),
                    'avg_ev': round(overall_stats['avg_ev'] or 0, 2),
                    'avg_stake': round(overall_stats['avg_stake'] or 0, 2)
                },
                'by_sport': sport_performance,
                'recent_form': recent_form
            }
    
    def get_timeline_data(self) -> List[Dict]:
        """
        Get data for timeline visualization (cumulative profit over time).
        
        Returns:
            List of data points for charting
        """
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    date_placed,
                    stake,
                    ev_percentage,
                    result,
                    payout,
                    event_name
                FROM bets
                ORDER BY date_placed ASC
            """)
            
            timeline_data = []
            cumulative_actual = 0
            cumulative_expected = 0
            
            for row in cursor:
                bet = dict(row)
                
                # Calculate expected value for this bet
                expected_profit = bet['stake'] * (bet['ev_percentage'] / 100)
                cumulative_expected += expected_profit
                
                # Calculate actual profit/loss
                if bet['result'] == 'win':
                    actual_profit = bet['payout']
                elif bet['result'] == 'loss':
                    actual_profit = -bet['stake']
                else:
                    actual_profit = 0  # Pending, push, or void
                
                cumulative_actual += actual_profit
                
                timeline_data.append({
                    'date': bet['date_placed'],
                    'cumulative_expected': round(cumulative_expected, 2),
                    'cumulative_actual': round(cumulative_actual, 2),
                    'event': bet['event_name'],
                    'result': bet['result'],
                    'stake': bet['stake'],
                    'ev': bet['ev_percentage']
                })
            
            return timeline_data

# Initialize database
db = BetDatabase()

# API Routes
@bet_tracker_bp.route('/')
def tracker_page():
    """Render the bet tracker page"""
    return render_template('tracker.html')

@bet_tracker_bp.route('/api/bets', methods=['GET'])
def get_bets():
    """
    API endpoint to retrieve bets with optional filters.
    
    Query Parameters:
        start_date: Filter bets after this date
        end_date: Filter bets before this date
        result: Filter by result status
        limit: Maximum number of records
        
    Returns:
        JSON response with list of bets
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        result_filter = request.args.get('result')
        limit = request.args.get('limit', type=int)
        
        bets = db.get_all_bets(start_date, end_date, result_filter, limit)
        
        return jsonify({
            'success': True,
            'bets': bets,
            'count': len(bets)
        })
        
    except Exception as e:
        logger.error(f"Error retrieving bets: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bet_tracker_bp.route('/api/bets', methods=['POST'])
def add_bet():
    """
    API endpoint to add a new bet.
    
    Request Body:
        JSON object with bet details
        
    Returns:
        JSON response with created bet ID
    """
    try:
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['date_placed', 'sport', 'event_name', 'market_type',
                          'selection', 'bookmaker', 'odds', 'stake', 'ev_percentage']
        
        for field in required_fields:
            if field not in data:
                return jsonify({'success': False, 'error': f'Missing field: {field}'}), 400
        
        # Create BetRecord
        bet = BetRecord(
            date_placed=data['date_placed'],
            sport=data['sport'],
            event_name=data['event_name'],
            market_type=data['market_type'],
            selection=data['selection'],
            bookmaker=data['bookmaker'],
            odds=float(data['odds']),
            stake=float(data['stake']),
            ev_percentage=float(data['ev_percentage']),
            implied_prob=float(data.get('implied_prob', 0)),
            consensus_prob=float(data.get('consensus_prob', 0)),
            result=data.get('result', BetResult.PENDING.value),
            payout=float(data.get('payout', 0)),
            notes=data.get('notes', '')
        )
        
        # Add to database
        bet_id = db.add_bet(bet)
        
        return jsonify({
            'success': True,
            'bet_id': bet_id,
            'message': 'Bet added successfully'
        })
        
    except Exception as e:
        logger.error(f"Error adding bet: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bet_tracker_bp.route('/api/bets/<int:bet_id>', methods=['PUT'])
def update_bet(bet_id: int):
    """
    API endpoint to update an existing bet.
    
    Args:
        bet_id: ID of the bet to update
        
    Request Body:
        JSON object with fields to update
        
    Returns:
        JSON response indicating success or failure
    """
    try:
        data = request.get_json()
        
        # Calculate payout if result is being updated
        if 'result' in data:
            bet = db.get_bet(bet_id)
            if bet:
                if data['result'] == 'win':
                    # Calculate winning payout
                    if bet['odds'] > 0:
                        data['payout'] = bet['stake'] * (bet['odds'] / 100)
                    else:
                        data['payout'] = bet['stake'] * (100 / abs(bet['odds']))
                elif data['result'] == 'push':
                    data['payout'] = 0
                elif data['result'] == 'loss':
                    data['payout'] = -bet['stake']
        
        success = db.update_bet(bet_id, data)
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Bet updated successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Bet not found or no valid updates'
            }), 404
            
    except Exception as e:
        logger.error(f"Error updating bet: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bet_tracker_bp.route('/api/bets/<int:bet_id>', methods=['DELETE'])
def delete_bet(bet_id: int):
    """
    API endpoint to delete a bet.
    
    Args:
        bet_id: ID of the bet to delete
        
    Returns:
        JSON response indicating success or failure
    """
    try:
        success = db.delete_bet(bet_id)
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Bet deleted successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Bet not found'
            }), 404
            
    except Exception as e:
        logger.error(f"Error deleting bet: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bet_tracker_bp.route('/api/statistics', methods=['GET'])
def get_statistics():
    """
    API endpoint to get betting statistics.
    
    Returns:
        JSON response with comprehensive statistics
    """
    try:
        stats = db.get_statistics()
        return jsonify({
            'success': True,
            'statistics': stats
        })
        
    except Exception as e:
        logger.error(f"Error getting statistics: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bet_tracker_bp.route('/api/timeline', methods=['GET'])
def get_timeline():
    """
    API endpoint to get timeline data for visualization.
    
    Returns:
        JSON response with timeline data points
    """
    try:
        timeline = db.get_timeline_data()
        return jsonify({
            'success': True,
            'timeline': timeline
        })
        
    except Exception as e:
        logger.error(f"Error getting timeline: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bet_tracker_bp.route('/api/import-opportunity', methods=['POST'])
def import_opportunity():
    """
    API endpoint to import a bet opportunity from the main analyzer.
    This creates a pending bet record from opportunity data.
    
    Request Body:
        JSON object with opportunity details
        
    Returns:
        JSON response with created bet ID
    """
    try:
        data = request.get_json()
        
        # Convert opportunity format to bet format
        bet = BetRecord(
            date_placed=datetime.now(timezone.utc).isoformat(),
            sport=data.get('sport_title', 'Unknown'),
            event_name=data.get('event_name', ''),
            market_type=data.get('market_type', ''),
            selection=data.get('selection', ''),
            bookmaker=data.get('target_book', ''),
            odds=float(data.get('target_odds', 0)),
            stake=float(data.get('recommended_bet_size', 0)),
            ev_percentage=float(data.get('ev_percentage', 0)),
            implied_prob=float(data.get('target_implied_prob', 0)),
            consensus_prob=float(data.get('consensus_prob', 0)),
            result=BetResult.PENDING.value,
            payout=0,
            notes='Imported from analyzer'
        )
        
        bet_id = db.add_bet(bet)
        
        return jsonify({
            'success': True,
            'bet_id': bet_id,
            'message': 'Opportunity imported as pending bet'
        })
        
    except Exception as e:
        logger.error(f"Error importing opportunity: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bet_tracker_bp.route('/api/seed-demo-data', methods=['POST'])
def seed_demo_data():
    """
    API endpoint to seed the database with demo data for testing.
    
    Returns:
        JSON response indicating number of records created
    """
    try:
        # Clear existing demo data (optional)
        # db.clear_all()  # Implement if needed
        
        sports = ['NFL', 'NBA', 'MLB', 'NHL', 'NCAA Basketball']
        bookmakers = ['DraftKings', 'FanDuel', 'BetMGM', 'Caesars']
        market_types = ['moneyline', 'spread', 'total']
        
        demo_bets = []
        current_date = datetime.now(timezone.utc)
        
        # Generate 50 demo bets over the last 30 days
        for i in range(50):
            days_ago = random.randint(0, 30)
            bet_date = current_date - timedelta(days=days_ago)
            
            sport = random.choice(sports)
            odds = random.choice([-150, -120, -110, 110, 120, 150, 180, 200])
            stake = random.choice([10, 20, 25, 50, 100])
            ev_percentage = random.uniform(2.0, 8.0)
            
            # Determine result based on EV (higher EV = better chance to win)
            if days_ago > 2:  # Only resolved bets older than 2 days
                win_probability = 0.45 + (ev_percentage / 100)  # Base 45% + EV boost
                result = 'win' if random.random() < win_probability else 'loss'
                
                if result == 'win':
                    if odds > 0:
                        payout = stake * (odds / 100)
                    else:
                        payout = stake * (100 / abs(odds))
                else:
                    payout = -stake
            else:
                result = 'pending'
                payout = 0
            
            bet = BetRecord(
                date_placed=bet_date.isoformat(),
                sport=sport,
                event_name=f"Team A vs Team B - Demo {i+1}",
                market_type=random.choice(market_types),
                selection=random.choice(['Team A', 'Team B', 'Over', 'Under']),
                bookmaker=random.choice(bookmakers),
                odds=odds,
                stake=stake,
                ev_percentage=round(ev_percentage, 2),
                implied_prob=round(random.uniform(40, 60), 2),
                consensus_prob=round(random.uniform(45, 55), 2),
                result=result,
                payout=round(payout, 2),
                notes=f"Demo bet #{i+1}"
            )
            
            db.add_bet(bet)
            demo_bets.append(asdict(bet))
        
        return jsonify({
            'success': True,
            'message': f'Created {len(demo_bets)} demo bets',
            'sample': demo_bets[:5]  # Return first 5 as sample
        })
        
    except Exception as e:
        logger.error(f"Error seeding demo data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Integration helper for main app
def integrate_with_app(app):
    """
    Helper function to integrate bet tracker with main Flask app.
    
    Args:
        app: Flask application instance
    
    Usage in main app.py:
        from bet_tracker import bet_tracker_bp, integrate_with_app
        integrate_with_app(app)
    """
    app.register_blueprint(bet_tracker_bp)
    logger.info("Bet Tracker module integrated successfully")

# Export for use in main app
__all__ = ['bet_tracker_bp', 'integrate_with_app', 'BetDatabase', 'BetRecord', 'BetResult']
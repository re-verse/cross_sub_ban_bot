"""
Database utilities for Cross-Sub Ban Bot
SQLite replacement for Google Sheets

Column mapping from Google Sheets:
1. Username
2. SourceSub  
3. Reason
4. Timestamp
5. ManualOverride
6. LogID
7. ModeratorName
8. ModSub
9. ForgiveTimestamp
10. ExemptSubs
"""

import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import json


class BanDatabase:
    def __init__(self, db_path: str = "bans.db"):
        """Initialize the SQLite database connection."""
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Create the database and tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE,
                    source_sub TEXT NOT NULL,
                    reason TEXT,
                    timestamp TEXT NOT NULL,
                    manual_override TEXT DEFAULT '',
                    log_id TEXT,
                    moderator_name TEXT,
                    mod_sub TEXT DEFAULT '',
                    forgive_timestamp TEXT DEFAULT '',
                    exempt_subs TEXT DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes for performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_username ON bans(username)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source_sub ON bans(source_sub)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON bans(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_override ON bans(manual_override)")
            
    def get_all_records(self) -> List[Dict]:
        """
        Replace sheet.get_all_records() - get all ban records
        Returns list of dictionaries matching the Google Sheets format
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT username, source_sub, reason, timestamp, manual_override,
                       log_id, moderator_name, mod_sub, forgive_timestamp, exempt_subs
                FROM bans 
                ORDER BY timestamp DESC
            """)
            
            records = []
            for row in cursor.fetchall():
                records.append({
                    'Username': row['username'],
                    'SourceSub': row['source_sub'],
                    'Reason': row['reason'],
                    'Timestamp': row['timestamp'],
                    'ManualOverride': row['manual_override'],
                    'LogID': row['log_id'],
                    'ModeratorName': row['moderator_name'],
                    'ModSub': row['mod_sub'],
                    'ForgiveTimestamp': row['forgive_timestamp'],
                    'ExemptSubs': row['exempt_subs']
                })
            
            return records
    
    def append_row(self, row_data: List) -> bool:
        """
        Replace sheet.append_row() - add new ban record
        row_data format: [user, source, reason, timestamp, manual_override, log_id, mod, mod_sub, forgive_timestamp, exempt_subs]
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO bans (username, source_sub, reason, timestamp, manual_override,
                                    log_id, moderator_name, mod_sub, forgive_timestamp, exempt_subs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row_data)
                return True
        except Exception as e:
            print(f"[ERROR] Failed to insert ban record: {e}")
            return False
    
    def update_forgiveness(self, username: str, source_sub: str, moderator: str, 
                          mod_sub: str, forgive_time: str) -> bool:
        """
        Replace sheet.update_cell() calls for forgiveness
        Mark user as forgiven when unbanned by source subreddit
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    UPDATE bans 
                    SET manual_override = 'yes',
                        moderator_name = ?,
                        mod_sub = ?,
                        forgive_timestamp = ?
                    WHERE username = ? COLLATE NOCASE 
                      AND source_sub = ?
                      AND manual_override != 'yes'
                """, (moderator, mod_sub, forgive_time, username, source_sub))
                
                if cursor.rowcount > 0:
                    print(f"[DB] Updated {cursor.rowcount} records for forgiveness: {username}")
                    return True
                else:
                    print(f"[DB] No records found to update for {username} from {source_sub}")
                    return False
                    
        except Exception as e:
            print(f"[ERROR] Failed to update forgiveness for {username}: {e}")
            return False
    
    def update_exempt_subs(self, username: str, exempt_subs: str) -> bool:
        """
        Replace sheet.update_cell() for exempt subs
        Update exempt subreddits for a user
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    UPDATE bans 
                    SET exempt_subs = ?
                    WHERE username = ? COLLATE NOCASE
                """, (exempt_subs, username))
                
                return cursor.rowcount > 0
                
        except Exception as e:
            print(f"[ERROR] Failed to update exempt subs for {username}: {e}")
            return False
    
    def get_recent_entries(self, source_sub: str, hours: int = 24) -> int:
        """
        Get count of recent entries from a source subreddit
        Used for rate limiting
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT COUNT(*) 
                FROM bans 
                WHERE source_sub = ? AND timestamp > ?
            """, (source_sub, cutoff_str))
            
            return cursor.fetchone()[0]
    
    def cleanup_old_records(self, retention_days: int = 10) -> int:
        """
        Clean up old records based on retention policy
        Returns number of deleted records
        """
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                DELETE FROM bans 
                WHERE timestamp < ? AND manual_override != 'yes'
            """, (cutoff_str,))
            
            deleted_count = cursor.rowcount
            print(f"[DB] Cleaned up {deleted_count} old records (older than {retention_days} days)")
            return deleted_count
    
    def export_to_json(self, output_file: str = "ban_export.json"):
        """Export all data to JSON format for backup/migration"""
        records = self.get_all_records()
        with open(output_file, 'w') as f:
            json.dump(records, f, indent=2)
        print(f"[DB] Exported {len(records)} records to {output_file}")
        return len(records)
    
    def import_from_json(self, input_file: str) -> int:
        """Import data from JSON format (for migration from Google Sheets)"""
        with open(input_file, 'r') as f:
            records = json.load(f)
        
        imported = 0
        with sqlite3.connect(self.db_path) as conn:
            for record in records:
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO bans 
                        (username, source_sub, reason, timestamp, manual_override,
                         log_id, moderator_name, mod_sub, forgive_timestamp, exempt_subs)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        record.get('Username', ''),
                        record.get('SourceSub', ''),
                        record.get('Reason', ''),
                        record.get('Timestamp', ''),
                        record.get('ManualOverride', ''),
                        record.get('LogID', ''),
                        record.get('ModeratorName', ''),
                        record.get('ModSub', ''),
                        record.get('ForgiveTimestamp', ''),
                        record.get('ExemptSubs', '')
                    ))
                    imported += 1
                except Exception as e:
                    print(f"[ERROR] Failed to import record {record}: {e}")
        
        print(f"[DB] Imported {imported} records from {input_file}")
        return imported
    
    def get_stats(self) -> Dict:
        """Get database statistics"""
        with sqlite3.connect(self.db_path) as conn:
            # Total records
            total = conn.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
            
            # Active bans (not forgiven)
            active = conn.execute("""
                SELECT COUNT(*) FROM bans 
                WHERE manual_override != 'yes'
            """).fetchone()[0]
            
            # Records by source sub
            cursor = conn.execute("""
                SELECT source_sub, COUNT(*) 
                FROM bans 
                GROUP BY source_sub 
                ORDER BY COUNT(*) DESC
            """)
            by_sub = dict(cursor.fetchall())
            
            # Recent activity (last 24h)
            cutoff = (datetime.utcnow() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
            recent = conn.execute("""
                SELECT COUNT(*) FROM bans 
                WHERE timestamp > ?
            """, (cutoff,)).fetchone()[0]
            
            return {
                'total_records': total,
                'active_bans': active,
                'forgiven_bans': total - active,
                'records_by_sub': by_sub,
                'recent_24h': recent
            }


# Compatibility layer - replace the Google Sheets functions
def setup_database(db_path: str = "bans.db") -> BanDatabase:
    """
    Replace setup_google_sheet() function
    Returns a BanDatabase instance instead of sheet, client
    """
    print(f"[INFO] SQLite database '{db_path}' initialized.")
    return BanDatabase(db_path)


# For testing and development
if __name__ == "__main__":
    # Test the database
    db = BanDatabase("test_bans.db")
    
    # Test adding a record
    test_row = [
        "testuser", 
        "r/testhockey", 
        "Auto XSub Pact Ban", 
        datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
        "",  # manual_override
        "test123",  # log_id
        "testmod",  # moderator
        "",  # mod_sub
        "",  # forgive_timestamp
        ""   # exempt_subs
    ]
    
    db.append_row(test_row)
    records = db.get_all_records()
    print(f"Test: Added record, now have {len(records)} total records")
    
    # Test stats
    stats = db.get_stats()
    print("Database stats:", stats)

#!/usr/bin/env python3
"""
Google Sheets to SQLite Migration Script
For Cross-Sub Ban Bot

This script will:
1. Connect to your Google Sheets
2. Export all data to JSON
3. Import into SQLite database
4. Verify the migration was successful
"""

import os
import sys
import json
import time
from datetime import datetime

# Import existing Google Sheets setup
from bot_config import setup_google_sheet
from db_utils import BanDatabase


def export_google_sheets_data(output_file="sheets_export.json"):
    """
    Export all data from Google Sheets to JSON file
    """
    print("[MIGRATION] Step 1: Connecting to Google Sheets...")
    
    try:
        sheet, client, sheet_key = setup_google_sheet()
        print(f"[MIGRATION] Connected to sheet: {sheet_key}")
        
        print("[MIGRATION] Step 2: Downloading all records...")
        start_time = time.time()
        records = sheet.get_all_records()
        load_time = time.time() - start_time
        
        print(f"[MIGRATION] Downloaded {len(records)} records in {load_time:.2f}s")
        
        # Save to JSON file
        with open(output_file, 'w') as f:
            json.dump(records, f, indent=2)
        
        print(f"[MIGRATION] Exported data to {output_file}")
        
        # Show sample data for verification
        if records:
            print(f"[MIGRATION] Sample record:")
            sample = records[0]
            for key, value in sample.items():
                print(f"  {key}: {value}")
        
        return records, output_file
        
    except Exception as e:
        print(f"[ERROR] Failed to export Google Sheets data: {e}")
        return None, None


def import_to_sqlite(json_file, db_file="bans.db"):
    """
    Import JSON data into SQLite database
    """
    print(f"[MIGRATION] Step 3: Creating SQLite database: {db_file}")
    
    # Initialize database
    db = BanDatabase(db_file)
    
    print(f"[MIGRATION] Step 4: Importing data from {json_file}...")
    imported_count = db.import_from_json(json_file)
    
    return db, imported_count


def verify_migration(json_file, db_file="bans.db"):
    """
    Verify that migration was successful by comparing data
    """
    print("[MIGRATION] Step 5: Verifying migration...")
    
    # Load original JSON data
    with open(json_file, 'r') as f:
        original_records = json.load(f)
    
    # Load SQLite data
    db = BanDatabase(db_file)
    sqlite_records = db.get_all_records()
    
    print(f"[VERIFICATION] Original records: {len(original_records)}")
    print(f"[VERIFICATION] SQLite records: {len(sqlite_records)}")
    
    if len(original_records) == len(sqlite_records):
        print("✅ [VERIFICATION] Record counts match!")
    else:
        print("❌ [VERIFICATION] Record count mismatch!")
        return False
    
    # Check a few sample records
    if original_records and sqlite_records:
        orig_usernames = {r.get('Username', '').lower() for r in original_records[:10]}
        sqlite_usernames = {r.get('Username', '').lower() for r in sqlite_records[:10]}
        
        if orig_usernames == sqlite_usernames:
            print("✅ [VERIFICATION] Sample usernames match!")
        else:
            print("❌ [VERIFICATION] Sample username mismatch!")
            print(f"  Original: {orig_usernames}")
            print(f"  SQLite: {sqlite_usernames}")
    
    # Show database stats
    stats = db.get_stats()
    print(f"[VERIFICATION] Database statistics:")
    print(f"  Total records: {stats['total_records']}")
    print(f"  Active bans: {stats['active_bans']}")
    print(f"  Forgiven bans: {stats['forgiven_bans']}")
    print(f"  Recent activity (24h): {stats['recent_24h']}")
    
    return True


def create_backup(db_file="bans.db"):
    """
    Create a backup of the new database
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = f"bans_backup_{timestamp}.db"
    
    import shutil
    shutil.copy2(db_file, backup_file)
    print(f"[BACKUP] Created backup: {backup_file}")
    return backup_file


def main():
    """
    Run the complete migration process
    """
    print("="*60)
    print("Google Sheets → SQLite Migration")
    print("Cross-Sub Ban Bot Database Migration")
    print("="*60)
    
    # Check if we're in the right directory
    if not os.path.exists('bot_config.py'):
        print("[ERROR] bot_config.py not found. Run this script from the bot directory.")
        sys.exit(1)
    
    try:
        # Step 1: Export from Google Sheets
        records, json_file = export_google_sheets_data()
        if not records:
            print("[ERROR] Failed to export Google Sheets data")
            sys.exit(1)
        
        # Step 2: Import to SQLite
        db, imported_count = import_to_sqlite(json_file)
        print(f"[MIGRATION] Successfully imported {imported_count} records")
        
        # Step 3: Verify migration
        if verify_migration(json_file):
            print("✅ [SUCCESS] Migration completed successfully!")
        else:
            print("❌ [ERROR] Migration verification failed!")
            sys.exit(1)
        
        # Step 4: Create backup
        backup_file = create_backup()
        
        print("\n" + "="*60)
        print("MIGRATION COMPLETE!")
        print("="*60)
        print(f"✅ Exported: {json_file}")
        print(f"✅ Database: bans.db")
        print(f"✅ Backup: {backup_file}")
        print(f"✅ Records migrated: {imported_count}")
        print("\nNext steps:")
        print("1. Test the new SQLite version of the bot")
        print("2. Update bot_config.py to use SQLite")
        print("3. Remove Google Sheets dependencies")
        print("\nFiles you can safely keep:")
        print(f"- {json_file} (original data backup)")
        print(f"- {backup_file} (SQLite backup)")
        
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Migration cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Migration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

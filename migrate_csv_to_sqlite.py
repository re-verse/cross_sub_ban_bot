#!/usr/bin/env python3
"""
CSV to SQLite Migration Script
For Cross-Sub Ban Bot

Simple migration from Google Sheets CSV export to SQLite

Steps to use this script:
1. Go to your Google Sheets
2. File > Download > Comma Separated Values (.csv)
3. Save as 'bans_export.csv' in this directory
4. Run this script: python3 migrate_csv_to_sqlite.py
"""

import os
import sys
import csv
import json
import time
from datetime import datetime
from db_utils import BanDatabase


def detect_csv_format(csv_file):
    """
    Detect the format of the CSV file and show preview
    """
    print(f"[MIGRATION] Analyzing CSV file: {csv_file}")
    
    if not os.path.exists(csv_file):
        print(f"[ERROR] CSV file not found: {csv_file}")
        print("\nTo create the CSV file:")
        print("1. Open your Google Sheets")
        print("2. File > Download > Comma Separated Values (.csv)")
        print("3. Save as 'bans_export.csv' in this directory")
        return None, None
    
    with open(csv_file, 'r', encoding='utf-8') as f:
        # Try to detect the dialect
        sample = f.read(2048)
        f.seek(0)
        sniffer = csv.Sniffer()
        
        try:
            dialect = sniffer.sniff(sample)
            print(f"[MIGRATION] Detected delimiter: '{dialect.delimiter}'")
        except:
            dialect = None
            print("[MIGRATION] Using default CSV format")
        
        # Read the header and first few rows
        reader = csv.reader(f, dialect=dialect) if dialect else csv.reader(f)
        
        rows = []
        for i, row in enumerate(reader):
            rows.append(row)
            if i >= 5:  # Read header + 5 data rows
                break
    
    if not rows:
        print("[ERROR] CSV file is empty")
        return None, None
    
    headers = rows[0]
    data_rows = rows[1:] if len(rows) > 1 else []
    
    print(f"[MIGRATION] Found {len(headers)} columns:")
    for i, header in enumerate(headers, 1):
        print(f"  {i}. {header}")
    
    if data_rows:
        print(f"\n[MIGRATION] Sample data (first row):")
        for i, (header, value) in enumerate(zip(headers, data_rows[0]), 1):
            print(f"  {header}: {value}")
    
    return headers, rows

def map_columns_to_schema(headers):
    """
    Map CSV columns to our database schema
    Expected columns: Username, SourceSub, Reason, Timestamp, ManualOverride, LogID, ModeratorName, ModSub, ForgiveTimestamp, ExemptSubs
    """
    print(f"\n[MIGRATION] Mapping columns to database schema...")
    
    # Expected database columns
    db_columns = [
        'Username', 'SourceSub', 'Reason', 'Timestamp', 'ManualOverride',
        'LogID', 'ModeratorName', 'ModSub', 'ForgiveTimestamp', 'ExemptSubs'
    ]
    
    # Try to auto-map columns (case insensitive)
    column_mapping = {}
    
    for db_col in db_columns:
        # Try exact match first
        for i, header in enumerate(headers):
            if header.strip().lower() == db_col.lower():
                column_mapping[db_col] = i
                print(f"  ✅ {db_col} -> Column {i+1} ({header})")
                break
        else:
            # Try partial matches
            for i, header in enumerate(headers):
                if db_col.lower() in header.strip().lower():
                    column_mapping[db_col] = i
                    print(f"  ⚠️  {db_col} -> Column {i+1} ({header}) [partial match]")
                    break
            else:
                # No match found
                column_mapping[db_col] = None
                print(f"  ❌ {db_col} -> Not found")
    
    return column_mapping

def convert_csv_to_sqlite(csv_file, db_file="bans.db"):
    """
    Convert CSV data to SQLite database
    """
    print(f"\n[MIGRATION] Converting {csv_file} to {db_file}...")
    
    # Analyze CSV format
    headers, all_rows = detect_csv_format(csv_file)
    if not headers:
        return False
    
    # Map columns
    column_mapping = map_columns_to_schema(headers)
    
    # Check if we have minimum required columns
    required_cols = ['Username', 'SourceSub', 'Timestamp']
    missing_required = [col for col in required_cols if column_mapping.get(col) is None]
    
    if missing_required:
        print(f"[ERROR] Missing required columns: {missing_required}")
        print("Please ensure your CSV has at least: Username, SourceSub, Timestamp")
        return False
    
    # Create SQLite database
    db = BanDatabase(db_file)
    
    # Process data rows
    data_rows = all_rows[1:]  # Skip header
    imported_count = 0
    error_count = 0
    
    print(f"\n[MIGRATION] Processing {len(data_rows)} rows...")
    
    for row_num, row in enumerate(data_rows, 2):  # Start at 2 (1=header)
        try:
            # Extract values using column mapping
            def get_value(col_name, default=''):
                col_index = column_mapping.get(col_name)
                if col_index is None:
                    return default
                if col_index >= len(row):
                    return default
                return row[col_index].strip() if row[col_index] else default
            
            # Build row data for database
            row_data = [
                get_value('Username'),
                get_value('SourceSub'),
                get_value('Reason', 'Auto XSub Pact Ban'),
                get_value('Timestamp'),
                get_value('ManualOverride'),
                get_value('LogID'),
                get_value('ModeratorName'),
                get_value('ModSub'),
                get_value('ForgiveTimestamp'),
                get_value('ExemptSubs')
            ]
            
            # Skip empty rows
            if not row_data[0]:  # No username
                continue
            
            # Import to database
            success = db.append_row(row_data)
            if success:
                imported_count += 1
                if imported_count % 100 == 0:
                    print(f"  Processed {imported_count} records...")
            else:
                error_count += 1
                print(f"  [ERROR] Failed to import row {row_num}: {row_data[0]}")
                
        except Exception as e:
            error_count += 1
            print(f"  [ERROR] Row {row_num}: {e}")
    
    print(f"\n[MIGRATION] Import completed!")
    print(f"  ✅ Imported: {imported_count} records")
    print(f"  ❌ Errors: {error_count} records")
    
    return imported_count > 0

def verify_migration(db_file="bans.db"):
    """
    Verify the migration was successful
    """
    print(f"\n[MIGRATION] Verifying database: {db_file}")
    
    db = BanDatabase(db_file)
    records = db.get_all_records()
    stats = db.get_stats()
    
    print(f"  📊 Total records: {len(records)}")
    print(f"  📊 Database stats: {stats}")
    
    if records:
        print(f"\n  🔍 Sample records:")
        for i, record in enumerate(records[:3], 1):
            print(f"    {i}. User: {record.get('Username', 'N/A')}, "
                  f"Source: {record.get('SourceSub', 'N/A')}, "
                  f"Time: {record.get('Timestamp', 'N/A')}")
    
    return len(records) > 0

def create_backup(db_file="bans.db"):
    """
    Create a backup of the database
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = f"bans_backup_{timestamp}.db"
    
    import shutil
    shutil.copy2(db_file, backup_file)
    print(f"[BACKUP] Created backup: {backup_file}")
    return backup_file

def main():
    """
    Run the CSV to SQLite migration
    """
    print("="*60)
    print("CSV → SQLite Migration")
    print("Cross-Sub Ban Bot Database Migration")
    print("="*60)
    
    csv_file = "bans_export.csv"
    db_file = "bans.db"
    
    # Check if CSV file exists
    if not os.path.exists(csv_file):
        print(f"[SETUP] CSV file not found: {csv_file}")
        print("\n📋 To create the CSV file:")
        print("1. Open your Google Sheets ban database")
        print("2. File > Download > Comma Separated Values (.csv)")
        print(f"3. Save as '{csv_file}' in this directory")
        print(f"4. Run this script again: python3 {os.path.basename(__file__)}")
        print("\n💡 Expected columns in your CSV:")
        print("   - Username (required)")
        print("   - SourceSub (required)")
        print("   - Timestamp (required)")
        print("   - Reason, ManualOverride, LogID, etc. (optional)")
        return
    
    try:
        print(f"[MIGRATION] Starting migration from {csv_file} to {db_file}")
        
        # Convert CSV to SQLite
        success = convert_csv_to_sqlite(csv_file, db_file)
        if not success:
            print("[ERROR] Migration failed!")
            return
        
        # Verify migration
        if verify_migration(db_file):
            print("✅ [SUCCESS] Migration verification passed!")
        else:
            print("❌ [ERROR] Migration verification failed!")
            return
        
        # Create backup
        backup_file = create_backup(db_file)
        
        # Success summary
        print("\n" + "="*60)
        print("MIGRATION COMPLETE! 🎉")
        print("="*60)
        print(f"✅ Source: {csv_file}")
        print(f"✅ Database: {db_file}")
        print(f"✅ Backup: {backup_file}")
        print("\n📋 Next steps:")
        print("1. Test the SQLite bot: python3 cross_sub_ban_bot_sqlite.py")
        print("2. Update config.json: set USE_SQLITE=true")
        print("3. Switch to SQLite version for production")
        print(f"\n🗄️ Files you can keep:")
        print(f"   - {csv_file} (original data)")
        print(f"   - {backup_file} (SQLite backup)")
        
    except Exception as e:
        print(f"[ERROR] Migration failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()

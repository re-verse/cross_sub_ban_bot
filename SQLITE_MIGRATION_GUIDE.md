py
```

#### Phase 4: Configuration Update (NEEDS PERMISSION 🛑)
```bash
# Update config to use SQLite
cp config_sqlite.json config.json
# Edit config.json: set "USE_SQLITE": true
```

#### Phase 5: Production Switch (NEEDS PERMISSION 🛑)
```bash
# Test the new bot version
python cross_sub_ban_bot_sqlite.py

# If successful, update main files
cp cross_sub_ban_bot_sqlite.py cross_sub_ban_bot.py
cp bot_config_sqlite.py bot_config.py
cp requirements_sqlite.txt requirements.txt
```

### Safety Features

#### Backups Created Automatically
- `sheets_export.json` - Original Google Sheets data
- `bans_backup_[timestamp].db` - SQLite database backup
- Original files preserved with `_sqlite` suffix

#### Rollback Plan
If anything goes wrong, simply:
1. Restore original `bot_config.py`
2. Set `USE_SQLITE: false` in config.json
3. Bot returns to Google Sheets mode

### Testing Results

Run `python test_sqlite_migration.py` to verify:
- ✅ Database operations work correctly
- ✅ Performance improvements achieved
- ✅ Compatibility with existing bot logic
- ✅ Data integrity maintained

### Configuration Options

#### config.json Settings
```json
{
  "USE_SQLITE": true,           // Enable SQLite backend
  "SQLITE_DB_PATH": "bans.db",  // Database file location
  "ROW_RETENTION_DAYS": 10      // Auto-cleanup old records
}
```

#### Environment Variables (unchanged)
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `REDDIT_USERNAME`
- `REDDIT_PASSWORD`

Google Sheets variables no longer needed after migration:
- ~~`GOOGLE_SERVICE_ACCOUNT_JSON`~~
- ~~`GOOGLE_SHEET_ID`~~

### CI/CD Integration Ready

The SQLite version is perfect for CI/CD pipelines:

#### GitHub Actions Benefits
- **Fast database operations** - no API delays
- **No external dependencies** - SQLite is built-in
- **Easy testing** - lightweight database
- **Better logging** - detailed performance metrics

#### Docker-Ready
```dockerfile
# Minimal dependencies
FROM python:3.9-slim
COPY requirements_sqlite.txt .
RUN pip install -r requirements_sqlite.txt
# SQLite included with Python - no additional setup needed
```

### Advanced Features

#### Built-in Statistics
```python
# Get comprehensive database stats
stats = database.get_stats()
print(f"Total bans: {stats['total_records']}")
print(f"Active bans: {stats['active_bans']}")
print(f"Recent activity: {stats['recent_24h']}")
```

#### Automatic Cleanup
```python
# Automatically remove old records
deleted = database.cleanup_old_records(retention_days=10)
print(f"Cleaned up {deleted} old records")
```

#### Performance Monitoring
```python
# Built-in timing for operations
start = time.time()
records = database.get_all_records()
print(f"Loaded {len(records)} records in {time.time()-start:.3f}s")
```

### Troubleshooting

#### Common Issues

**ImportError: No module named 'db_utils'**
- Solution: Run from bot directory where db_utils.py exists

**Database locked error**
- Solution: Ensure no other bot instances are running

**Permission denied on bans.db**
- Solution: Check file permissions, ensure write access

**Performance not improved**
- Solution: Verify USE_SQLITE=true in config.json

#### Debug Mode
```python
# Enable detailed logging
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Next Steps

After successful migration, consider:

1. **Remove Google Sheets dependencies**
   ```bash
   pip uninstall gspread oauth2client
   ```

2. **Set up CI/CD pipeline** 
   - GitHub Actions for automated testing
   - Docker containers for deployment
   - Monitoring and alerting

3. **Database optimization**
   - Regular vacuum operations
   - Index optimization
   - Backup automation

### Support

If you encounter any issues:

1. **Run the test suite**: `python test_sqlite_migration.py`
2. **Check the logs** for detailed error messages
3. **Verify configuration** in config.json
4. **Test rollback** to Google Sheets if needed

The migration is designed to be **safe, fast, and reversible**. All original data is preserved, and you can switch back to Google Sheets at any time.

---

**Ready to migrate?** Start with: `python test_sqlite_migration.py`

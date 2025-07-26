# SQLite Migration to Main Branch - Safety Plan

## Current Status
- **Main branch**: Runs Google Sheets bot every 20 minutes
- **sqlite-migration branch**: SQLite version tested and working

## Migration Strategy - SAFE APPROACH

### Option 1: Side-by-Side (RECOMMENDED)
Keep both bots running temporarily:
1. Rename old workflow: `run_bot.yml` → `run_bot_sheets.yml`
2. Add new workflow: `run_bot_sqlite.yml`
3. Both run on different schedules initially
4. Monitor SQLite version for a few days
5. Disable Sheets version when confident

### Option 2: Immediate Switch
Replace the bot entirely:
1. Update `run_bot.yml` to use SQLite version
2. Old bot stops, new bot starts
3. Riskier but cleaner

## Pre-Merge Checklist
- [x] SQLite bot tested locally
- [x] GitHub Actions working with secrets
- [x] Database migration completed (5 records)
- [ ] Update main workflow file
- [ ] Ensure bans.db is in .gitignore (if desired)
- [ ] Final test run

## Files to Update Before Merge
1. `.github/workflows/run_bot.yml` - Update to use SQLite
2. Remove test workflows after merge
3. Update README if needed

## Rollback Plan
If issues occur:
- Revert the workflow change
- Google Sheets bot resumes within 20 minutes
- No data loss (both systems independent)

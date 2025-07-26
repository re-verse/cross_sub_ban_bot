#!/bin/bash
# Check GitHub Actions status for the SQLite migration

echo "🔍 Checking GitHub Actions status..."
echo "Repository: re-verse/cross_sub_ban_bot"
echo "Branch: sqlite-migration"
echo ""
echo "Recent workflow runs:"
echo ""

# Using GitHub CLI if available
if command -v gh &> /dev/null; then
    gh run list --repo re-verse/cross_sub_ban_bot --branch sqlite-migration --limit 5
else
    echo "GitHub CLI not installed. Check manually at:"
    echo "https://github.com/re-verse/cross_sub_ban_bot/actions"
fi

echo ""
echo "To trigger another test, modify TRIGGER_TEST.md and push"

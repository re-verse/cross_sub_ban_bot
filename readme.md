# 🛡️ Cross-Sub Ban Bot

The Cross-Sub Ban Bot helps NHL subreddits enforce a pact:  
If a user is permanently banned for cross-subreddit trolling in one participating sub, they will be automatically banned from the others.

---

## 🚀 Setup Instructions

1. **Invite the bot** (`xsub-pact-bot`) to your subreddit.
   - It must have moderator permissions: **Access**, **Mail**, **Manage Bans**, **Mod Logs**.

2. **Create a Ban Reason**:
   - Ban reason text must exactly match: **Auto XSub Pact Ban** (or the configured reason).
   - Bans must be **permanent**.

3. **Ban users normally**:
   - When you ban a user for cross-sub trolling, select the **Auto XSub Pact Ban** reason.
   - The bot will automatically detect it and apply the same ban across other participating subreddits.

---

## ✍️ Forgiving a User

If you want to forgive a banned user:

- Send a modmail in your subreddit that says:
  
/xsub pardon u/username

- The pardon must come from a moderator of your subreddit (otherwise it will be ignored).
- Once forgiven, the user will be unbanned and kept forgiven unless they are banned again at a later time.

---

## 🔒 Bot Protections and Features

- **Daily Ban Limits**: No more than 30 bans from the same subreddit per day.
- **Trusted Sources Only**: Only bans from whitelisted subs are honored.
- **Forgiveness Persistence**: Forgiven users stay forgiven even if the sheet is refreshed.
- **Forgiveness Revocation**: If a new ban comes after forgiveness (by over 60 minutes), forgiveness is revoked and the user is re-banned.
- **Deleted Account Cleanup**: Deleted accounts are detected and automatically removed from the sheet after 24 hours.
- **Safe Operations**: Moderators and exempt users are never accidentally banned.

---

## 📋 Logs

Public ban and unban activity is logged at:

[https://re-verse.github.io/cross_sub_ban_bot/public_ban_log.md](https://re-verse.github.io/cross_sub_ban_bot/public_ban_log.md)




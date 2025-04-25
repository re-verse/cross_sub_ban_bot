import praw
import prawcore # Make sure to import prawcore for exceptions
import os
import sys # Import sys to potentially exit with error code

def test_reddit_connection():
    """Tests the connection to the Reddit API using provided credentials."""
    print("Attempting to connect to Reddit...")
    try:
        reddit = praw.Reddit(
            client_id=os.environ.get('CLIENT_ID'),
            client_secret=os.environ.get('CLIENT_SECRET'),
            username=os.environ.get('USERNAME'),
            password=os.environ.get('PASSWORD'),
            user_agent='YourBotName User Agent v1.0 (by /u/YourUsername)' # Customize your User-Agent
        )

        # Check if credentials seem okay initially by trying to get the authenticated user
        print("Checking authentication status...")
        user = reddit.user.me()

        if user:
            print(f"Successfully authenticated to Reddit as {user.name}")
        else:
            # This can happen even with correct client_id/secret if username/password is wrong
            # Or if the token couldn't be fetched properly for other reasons.
            print("Warning: Connection object created, but could not get authenticated user. Check USERNAME and PASSWORD.")
            # Depending on your needs, you might want to exit here if full auth is required.
            # sys.exit("Exiting due to authentication check failure.")

        # Try a simple API call that might require authentication scope
        print("Attempting to access subreddit r/all...")
        subreddit = reddit.subreddit('all')
        print(f"Accessed subreddit object: {subreddit.display_name}")

        # Try getting a listing - this often triggers token refresh/validation
        print("Attempting to retrieve hot posts from r/all (limit 5)...")
        posts = subreddit.hot(limit=5)

        count = 0
        for post in posts:
            print(f"\t- {post.title}")
            count += 1

        if count > 0:
             print(f"Successfully retrieved {count} hot posts from r/all.")
        else:
             print("Retrieved 0 posts. This might be normal or indicate an issue.")


    # --- CORRECTED EXCEPTION HANDLING ---
    except prawcore.exceptions.OAuthException as e:
        print(f"\n--- ERROR ---")
        print(f"OAuth error (Authentication/Authorization Failure).")
        print(f"Double-check ALL credentials (REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD) in GitHub Secrets.")
        print(f"Also verify your Reddit App type ('script') and permissions.")
        print(f"Error Details: {e}")
        sys.exit(1) # Exit with non-zero code to fail the workflow step

    except prawcore.exceptions.ResponseException as e:
        print(f"\n--- ERROR ---")
        print(f"Reddit API Response Error. Could be network issues, Reddit server problems, or permission denied (e.g., 403 Forbidden).")
        print(f"Check https://www.redditstatus.com/")
        print(f"Error Details: {e}")
        sys.exit(1) # Exit with non-zero code

    except Exception as e:
        print(f"\n--- UNEXPECTED ERROR ---")
        print(f"An unexpected error occurred: {type(e).__name__} - {e}")
        print("Check network connection, Reddit API status, credentials, and script logic.")
        sys.exit(1) # Exit with non-zero code

    print("\nScript finished.")

if __name__ == "__main__":
    # Basic check if environment variables seem to be missing
    required_vars = ['CLIENT_ID', 'CLIENT_SECRET', 'USERNAME', 'PASSWORD']
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        print(f"Error: Missing required environment variables: {', '.join(missing_vars)}")
        print("Ensure they are correctly set in the GitHub Actions workflow 'env' block mapping from secrets.")
        sys.exit(1)

    test_reddit_connection()

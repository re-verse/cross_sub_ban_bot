import praw
import os

def test_reddit_connection():
    try:
        reddit = praw.Reddit(
            client_id=os.environ['CLIENT_ID'],
            client_secret=os.environ['CLIENT_SECRET'],
            username=os.environ['USERNAME'],
            password=os.environ['PASSWORD'],
            user_agent='Test Script'
        )
        user = reddit.user.me()
        if user:
            print(f"Connected to Reddit as {user.name}")
        else:
            print("Connected, but couldn't get user. Check credentials.")
        subreddit = reddit.subreddit('all')
        print(f"Got subreddit: {subreddit.display_name}")
        posts = subreddit.hot(limit=5)
        print("Got hot posts from r/all")
        for post in posts:
            print(f"\t{post.title}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_reddit_connection()

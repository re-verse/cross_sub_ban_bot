        name: Reddit API Test  # Workflow name
        on:
          push:
            branches:
              - main  # Trigger on pushes to the main branch
          pull_request:
            branches:
              - main  # Trigger on pull requests to the main branch
        jobs:
          build:
            runs-on: ubuntu-latest  # Use an Ubuntu virtual machine
            steps:
              - uses: actions/checkout@v4  # Check out the repository code
              - name: Set up Python
                uses: actions/setup-python@v5
                with:
                  python-version: 3.x  # Use Python 3.x
              - name: Install dependencies
                run: |
                  python -m pip install --upgrade pip
                  pip install praw  # Install the PRAW library
              - name: Set environment variables
                run: |
                  echo "CLIENT_ID=$CLIENT_ID" >> $GITHUB_ENV
                  echo "CLIENT_SECRET=$CLIENT_SECRET" >> $GITHUB_ENV
                  echo "USERNAME=$USERNAME" >> $GITHUB_ENV
                  echo "PASSWORD=$PASSWORD" >> $GITHUB_ENV
                env:
                  CLIENT_ID: ${{ secrets.CLIENT_ID }}
                  CLIENT_SECRET: ${{ secrets.CLIENT_SECRET }}
                  USERNAME: ${{ secrets.USERNAME }}
                  PASSWORD: ${{ secrets.PASSWORD }}
              - name: Run Reddit API test
                run: python reddit_test.py  # Execute the test script
        

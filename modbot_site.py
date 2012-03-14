from flask import Flask
from flask import render_template

from models import *

app = Flask(__name__)


# main page
@app.route('/')
def main_page():
    return 'Future home of a web interface.'

@app.route('/r/<subreddit>/')
def show_subreddit(subreddit):
    s = Subreddit.query.filter_by(name=subreddit).first_or_404()
    return render_template('subreddit.html',s=s)

@app.route('/r/<subreddit>/log')
def show_subreddit_log(subreddit):
    s = Subreddit.query.filter_by(name=subreddit).first_or_404()
    return "it's log!"

@app.route('/c/<int:id>/')
def show_condition(id):
    c = Condition.query.filter_by(id=id).first_or_404()
    return c.notes

if __name__ == '__main__':
    app.debug = True
    app.run(port=5001)

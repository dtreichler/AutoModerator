from flask import Flask

app = Flask(__name__)


# main page
@app.route('/')
def main_page():
    return 'Future home of a web interface.'


if __name__ == '__main__':
    app.debug = True
    app.run()

# The Pico's main.py for the arm (deployed as :main.py): starts the Wi-Fi server at power-up.
# Nothing moves until a command arrives. Over USB, Ctrl-C stops the server (driven servos keep holding) and leaves the
# REPL, where "import arm" works as before. Needs secrets.py on the Pico (see armnet.py); without it, REPL only.
import armnet
armnet.run()

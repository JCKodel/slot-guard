# The /slots commands from a checkout. `make` alone lists them.
# Values go after the command, as in /slots: `make auto on`, `make agents 4`, `make evict 12 13`.
PYTHON ?= python3
GUARD := $(PYTHON) guard.py
COMMANDS := help status agents max auto quantum reset evict sprint sprint-off test

.DEFAULT_GOAL := help
.PHONY: $(COMMANDS)

# Words after the command are its values, not targets: they do nothing themselves.
# N=, ON=, SEQS= and M= still work.
ARGS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
ifneq ($(ARGS),)
$(eval $(filter-out $(COMMANDS),$(ARGS)):;@:)
endif
VALUE = $(or $(N),$(ON),$(SEQS),$(M),$(ARGS))

help:
	@echo 'Slot Guard from a checkout, the same as /slots in Claude Code:'
	@echo
	@echo '  make status            Show the limit, who is working and who waits    (/slots)'
	@echo '  make agents 4          Set the normal limit to N agents                 (/slots agents N)'
	@echo '  make max 6             Extra room above "agents" with auto on           (/slots max N)'
	@echo '  make auto on|off       Pace the limit by the usage windows, or fix it   (/slots auto on|off)'
	@echo '  make quantum 60        Seconds a slot is kept while others wait         (/slots quantum N)'
	@echo '  make reset             Forget usage readings, restart from "agents"     (/slots reset)'
	@echo '  make evict 12 13       Release tickets by number, as listed by status   (/slots evict N...)'
	@echo '  make sprint [5]        Every session starts at once, for M minutes      (/slots sprint all M)'
	@echo '  make sprint-off        End every sprint now                             (/slots sprint off)'
	@echo '  make test              Run the Python tests'

status:
	@$(GUARD) status

agents:
	@test -n "$(VALUE)" || { echo 'Usage: make agents 4'; exit 2; }
	@$(GUARD) configure --agents $(VALUE)

max:
	@test -n "$(VALUE)" || { echo 'Usage: make max 6'; exit 2; }
	@$(GUARD) configure --max $(VALUE)

auto:
	@test "$(VALUE)" = on -o "$(VALUE)" = off || { echo 'Usage: make auto on|off'; exit 2; }
	@$(GUARD) configure --auto $(VALUE)

quantum:
	@test -n "$(VALUE)" || { echo 'Usage: make quantum 60'; exit 2; }
	@$(GUARD) configure --quantum $(VALUE)

reset:
	@$(GUARD) reset

evict:
	@test -n "$(VALUE)" || { echo 'Usage: make evict 12 13'; exit 2; }
	@$(GUARD) evict $(VALUE)

sprint:
	@$(GUARD) sprint $(or $(VALUE),5)

sprint-off:
	@$(GUARD) sprint 0

test:
	@$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

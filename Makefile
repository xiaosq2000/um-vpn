# Install targets for um-vpn. There is no build step: `install` copies the two
# scripts onto PATH, `link` symlinks bin/ instead so a clone stays live.
#
#   make install               into ~/.local
#   make install PREFIX=/usr   system-wide (needs root)
#   make link                  development install: edits take effect at once
#   make uninstall             remove whatever the last install created
#   make test                  run the tests (headless Chrome, no Duo)
#   make check                 run pre-commit, then the tests
#
# Packagers: PREFIX and DESTDIR behave as usual. A staged build (DESTDIR set)
# writes no manifest, because there the package manager owns removal rather
# than `um-vpn uninstall`.
#
# Recipes are indented with hard tabs throughout, continuations included: make
# needs the first one, and .editorconfig rejects a stray space run.

PREFIX ?= $(HOME)/.local
XDG_DATA_HOME ?= $(HOME)/.local/share

BINDIR := $(DESTDIR)$(PREFIX)/bin
LIBEXECDIR := $(DESTDIR)$(PREFIX)/libexec/um-vpn
HELPER := libexec/um-vpn/browser-login.py

# The handshake between make and `um-vpn uninstall`: this has to stay the same
# path bin/um-vpn builds for DATA_DIR at the top of the script.
DATA_DIR := $(XDG_DATA_HOME)/um-vpn
MANIFEST := $(DATA_DIR)/manifest

.DEFAULT_GOAL := help
.PHONY: help install link uninstall remove-installed test check

help:
	@echo 'make install     copy um-vpn into $(PREFIX)'
	@echo 'make link        symlink it there instead, for development'
	@echo 'make uninstall   remove whatever the last install created'
	@echo 'make test        run the tests (headless Chrome, no Duo)'
	@echo 'make check       run pre-commit, then the tests'
	@echo
	@echo 'PREFIX and DESTDIR override where the files land.'

# $(1) is every path the install created, recorded without $(DESTDIR): the
# manifest describes where the files actually live, not where they were
# staged. Mode 700 matches how bin/um-vpn creates the same directory -- the
# Chrome profile is its neighbour.
define write-manifest
	@if [ -n '$(DESTDIR)' ]; then \
		echo 'staged build: no manifest written'; \
	else \
		install -d -m 700 '$(DATA_DIR)'; \
		{ \
			echo '# Written by make install / make link.'; \
			echo '# um-vpn uninstall removes exactly these.'; \
			for p in $(1); do echo "$$p"; done; \
		} >'$(MANIFEST)'; \
	fi
endef

install: remove-installed
	install -d '$(BINDIR)' '$(LIBEXECDIR)'
	install -m 755 bin/um-vpn '$(BINDIR)/um-vpn'
	install -m 755 '$(HELPER)' '$(LIBEXECDIR)/browser-login.py'
	$(call write-manifest,$(PREFIX)/bin/um-vpn $(PREFIX)/libexec/um-vpn)
	@echo 'installed: $(BINDIR)/um-vpn'

# Development install. Only bin/ is linked: um-vpn resolves its own path with
# `readlink -f`, so the helper lookup lands back in this clone's libexec/ and
# both files stay whatever the working tree says they are.
link: remove-installed
	install -d '$(BINDIR)'
	ln -sfn '$(CURDIR)/bin/um-vpn' '$(BINDIR)/um-vpn'
	$(call write-manifest,$(PREFIX)/bin/um-vpn)
	@echo 'linked: $(BINDIR)/um-vpn -> $(CURDIR)/bin/um-vpn'

uninstall: remove-installed
	@echo 'Removed the installed files.'
	@echo
	@echo 'The cached cookie, the Chrome profile and any saved UMPASS'
	@echo 'credentials are still here. To remove those too -- and to'
	@echo 'disconnect a tunnel that is still up -- run this from the'
	@echo 'clone first: ./bin/um-vpn uninstall'

# Clear a previous install before laying down a new one, so switching between
# `install` and `link` in either direction cannot strand a stale copy of the
# helper under libexec/. Every path is checked before it reaches `rm -rf`: the
# manifest is a file on disk, and a hand-edited one must not be able to aim
# this at somewhere that was never ours.
remove-installed:
	@if [ -z '$(DESTDIR)' ] && [ -f '$(MANIFEST)' ]; then \
		while IFS= read -r p || [ -n "$$p" ]; do \
			case "$$p" in \
				'' | '#'*) continue ;; \
				*/um-vpn | */um-vpn/*) rm -rf "$$p" ;; \
				*) echo "not ours: $$p" >&2; exit 1 ;; \
			esac; \
		done <'$(MANIFEST)'; \
		rm -f '$(MANIFEST)'; \
	fi

# The tests drive a real headless Chrome against a stand-in portal on
# 127.0.0.1, with no Duo, no network and no sudo, so they are safe to run on
# any change. Kept out of the pre-commit hook: they take ~20 seconds.
test:
	python3 -m unittest discover -s tests -v

check:
	pre-commit run --all-files
	$(MAKE) --no-print-directory test

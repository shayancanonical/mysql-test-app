#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Encapsulation of the charm's state machine."""

import codecs
import pickle

from ops import Object

import transitions


class StateMachine:
    states = [
        "waiting_for_database",
        "ready",
        "writing",
    ]

    state_transitions = [
        ("waiting_for_database", "ready"),
        ("ready", "writing"),
        ("writing", "ready"),
    ]

    blocking_states = [
        "waiting_for_database",
    ]

    def __init__(self):
        self.machine = transitions.Machine(
            model=self,
            states=self.states,
            initial="waiting_for_database",
        )

        for (source, destination) in self.state_transitions:
            self.machine.add_transition(trigger=f"to_{destination}", source=source, dest=destination)


class CharmState(Object):
    def __init__(self, charm):
        super().__init__(charm, "state-machine")

        self.charm = charm
        if not self.charm.unit_peer_data:
            return

        if self.charm.unit_peer_data.get("state-machine"):
            self.machine = pickle.loads(
                codecs.decode(self.charm.unit_peer_data.get("state-machine").encode(), "base64")
            )
        else:
            self.machine = StateMachine()

        self.framework.observe(
            self.charm.on.get_state_action, self._get_state_action
        )

    def __del__(self) -> None:
        """Store the state machine in the databag."""
        if not self.charm.unit_peer_data:
            return

        self.charm.unit_peer_data["state-machine"] = codecs.encode(
            pickle.dumps(self.machine), "base64"
        ).decode()

    @property
    def is_blocked(self) -> bool:
        """Indicate whether the state is blocked."""
        return self.machine.state in ["waiting_for_database"]

    def can_transition_to(self, state: str) -> bool:
        """Indicate whether the machine can transition to provided state."""
        if state not in self.machine.states:
            return False

        transition_check = getattr(self.machine, f"may_to_{state}")
        return transition_check()
    
    def transition_to(self, state: str) -> None:
        if not self.can_transition_to(state):
            raise Exception("Cannot transition to state")
        
        transition = getattr(self.machine, f"to_{state}")
        transition()

    def _get_state_action(self, event):
        event.set_results({
            "state": self.machine.state,
        })

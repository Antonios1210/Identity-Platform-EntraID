# Tests for upn.py: turning names into usernames.
# Every test has three parts: arrange, act, assert.

import upn

SUFFIX = "lab.local"


def test_first_choice_is_initial_dot_last():
    # Arrange: nobody has a username yet
    taken = set()
    # Act: generate Daniel's username
    name, collided = upn.generate("Daniel", "Okonkwo", SUFFIX, taken)
    # Assert: first choice, no collision
    assert name == "d.okonkwo@lab.local"
    assert collided is False


def test_collision_falls_back_to_full_first_name():
    # Arrange: Daniel already took d.okonkwo
    taken = {"d.okonkwo@lab.local"}
    # Act
    name, collided = upn.generate("Delphine", "Okonkwo", SUFFIX, taken)
    # Assert: second choice on her list, and a collision reported
    assert name == "delphine.okonkwo@lab.local"
    assert collided is True


def test_diacritics_are_transliterated():
    name, _ = upn.generate("Zoë", "Müller-Søndergaard", SUFFIX, set())
    assert name == "z.muller-sondergaard@lab.local"
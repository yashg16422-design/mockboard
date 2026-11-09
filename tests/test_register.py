"""Clear slang is flagged. Ordinary Hinglish is not."""

from mockboard.brain.register import register_hit


def test_over_familiar_address_and_abuse_are_hits():
    assert register_hit("Bro yeh toh bahut easy hai.") == "bro"
    assert register_hit("Yaar, tension mat le.") == "yaar"
    assert register_hit("yeh chutiya system hai") == "chutiya"
    assert register_hit("यार, छोड़ो") == "यार"


def test_ordinary_hinglish_is_not_a_hit():
    assert register_hit("Main logon ke liye seedha kuch badalna chahta hoon.") is None
    assert register_hit("Gaon ke bhai logon ko paani chahiye.") is None
    assert register_hit("Tumhare hisaab se kisan ko kya milega?") is None
    assert register_hit("Yaari se faisla nahi hota.") is None
    assert register_hit("") is None

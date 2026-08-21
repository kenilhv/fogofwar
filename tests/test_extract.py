from fogofwar.extract import extract_entities


def test_finds_multi_word_proper_noun():
    assert "Sam Ratnaparkhi" in extract_entities(
        "Sam Ratnaparkhi is the assigned owner of the migration."
    )


def test_finds_single_word_entity_mid_sentence():
    assert "Priya" in extract_entities("The ticket was reassigned to Priya yesterday.")


def test_ignores_sentence_initial_stopword():
    entities = extract_entities("The migration was blocked by AUTH-503.")
    assert "The" not in entities


def test_dedupes_within_text():
    entities = extract_entities("Bob owns it. Bob confirmed it. Bob is done.")
    assert entities.count("Bob") == 1


def test_empty_text_returns_empty():
    assert extract_entities("") == []


def test_no_proper_nouns_returns_empty():
    assert extract_entities("the service is still failing intermittently") == []

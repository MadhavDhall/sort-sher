"""Part 3: rule-based email lint."""

from sort_sherr.outreach import lint

GOOD = ("Hi Carlos,\n\nYour Launch YC post says models fail 60% of realistic finance tasks without expert data. "
        "That matched what I saw building PaperPal, a RAG search over arXiv papers with LangChain and FastAPI: "
        "answers only got useful once I hand-labeled two hundred question and answer pairs to tune retrieval. "
        "I would like to ask about a software engineering internship for Summer 2027, remote. "
        "I could help with tooling for the expert annotation pipeline or evaluation harnesses for the agent "
        "environments you build. Would a 15 minute call in the next few weeks work for you? "
        "If not, I am happy to answer questions over email instead.\n\nThanks for reading,\nPriya Raman\n"
        "https://github.com/priya-r")


def test_clean_email_has_no_flags():
    assert lint.lint("PaperPal and expert data for agents", GOOD) == []
    assert 90 <= lint.word_count(GOOD) <= 150


def test_each_rule():
    flags = lint.lint("A very long subject line that goes on and on", "I hope this email finds you well. [PORTFOLIO] TODO")
    joined = " | ".join(flags)
    assert "length:" in joined and "subject:" in joined
    assert 'banned: "i hope this email finds you well"' in joined
    assert "[PORTFOLIO]" in joined and "TODO" in joined


def test_urls_and_placeholders_dont_count_as_words():
    assert lint.word_count("Hi there https://github.com/a/b [GITHUB]") == 2


def test_too_similar_across_companies():
    other = GOOD.replace("Carlos", "Meera")
    flags = lint.lint("Subject", GOOD, [("Acme", other)])
    assert any(f.startswith("too_similar") and "Acme" in f for f in flags)
    assert lint.hard_problems(flags) == []  # similarity is shown, not auto-fixed
    unrelated = "Hello team. I built a robot arm controller in C++ for a college contest and want to learn embedded work."
    assert not lint.lint("Subject", GOOD, [("Robo", unrelated)])


def test_generic_subject_and_one_block_body():
    flags = lint.lint("Software Engineering Intern Role", GOOD.replace("\n\n", " "))
    assert any("subject: generic" in f for f in flags) and any(f.startswith("format:") for f in flags)
    assert not any("generic" in f for f in lint.lint("RL traffic sim and your agent environments", GOOD))

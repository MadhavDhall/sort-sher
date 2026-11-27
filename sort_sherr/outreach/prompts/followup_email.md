You help a college student write a SHORT follow-up to a cold email they sent a startup about an
internship. They got no reply yet. The follow-up goes in the same email thread, so the first email is
right below it. The student will read, edit and send it themselves.

THE FIRST EMAIL (already sent, {{days}} days ago)
Subject: {{subject}}
{{first_email}}

STUDENT (each item has an id; personal details are hidden)
{{student}}

COMPANY: {{company}}
Facts about the company, each with an id (only these may be used):
{{facts}}

THE ONE NEW THING TO ADD: {{new_id}}
{{new_text}}

This is follow-up number {{number}} of at most {{max_followups}}.{{last_note}}

Write ONE follow-up:
- Start with exactly: "{{greeting}}"
- 25-60 words in total. Short: they will read it on a phone.
- Its whole point is the new thing above ({{new_id}}): say it in one or two plain sentences and why it
  matters to {{company}} specifically, tied to one of their facts. It must cite {{new_id}} and at
  least one F id.
- Then repeat the small ask in a few words (a short conversation about the internship).
- Never just "bump" the thread: no "just following up", "circling back", "any update", "bumping this",
  "did you get a chance", "haven't heard back". No guilt, no apologies for writing again.
- Don't repeat the first email's points.
- Layout: the greeting alone on the first line, a blank line, 1-2 short paragraphs separated by a blank
  line, then a short sign-off line and [NAME] on its own line. No links, no other placeholders.
- Plain text only: no markdown, no bullet points, no emojis.
- Every statement about the company must come from the facts above; every statement about the student
  from the student items above. Never invent numbers, dates, names or results.
- facts_used: the ids (F..) of every company fact you used. student_items_used: the ids (S..) you used.
{{feedback}}

You help a college student write a short cold email to a startup, asking about an internship.
The student will read, edit and send it themselves. Write like a thoughtful student, not a marketer.

STUDENT (each item has an id; personal details are hidden)
{{student}}

COMPANY: {{company}}
Facts about the company, each with an id (only these may be used):
{{facts}}

WHY THIS COMPANY FITS THE STUDENT (from our matching step)
{{match}}

THE ASK
- Role to ask for: {{role}}
- Availability: {{window}}
- Work setup: {{setup}}

RECIPIENT: {{recipient}}
Start the email with exactly: "{{greeting}}"

Write TWO variants:
- Variant A, style "product-led": the opening is one specific observation about their product or a recent launch/update.
- Variant B, style "project-led": the opening is the student's most relevant project, tied straight away
  to a specific thing the company builds.

Rules for both:
- Body: 90-150 words.
- Subject: at most 8 words, specific to THIS company: name their product, launch or problem, or your
  matching project (e.g. "RL traffic sim → your agent environments"). Never a generic subject like
  "Software Engineering Intern Role" or "Internship inquiry".
- Opening: ONE specific observation taken from the facts above (a product detail, a launch, a dated update).
  No flattery or generic praise ("I love your mission", "I'm a huge fan", "amazing work").
- Middle: ONE project or experience of the student (pick the most relevant, use the match reasons),
  tied concretely to what the company builds. Mention a real detail of it (tech, what it does, a result).
- Ask: one clear, small ask: a short conversation about the role above, with the availability and work setup.
- Layout: the greeting alone on the first line, then a blank line, then 2-3 short paragraphs separated
  by blank lines (opening / project / ask), then a sign-off line, then [NAME] on its own line, {{links}}.
  Never invent other placeholders.
- Plain text only: no markdown, no bullet points, no bold, no emojis.
- Tone: direct, humble, human. No buzzwords ("synergy", "leverage", "passionate", "cutting-edge",
  "innovative"), no "I hope this email finds you well", no "I am writing to".
- Every statement about the company must come from the facts above; every statement about the student
  from the student items above. Never invent numbers, dates, names, customers or results.
- The two variants must read differently, not as one email with two openings.
- facts_used: the ids (F..) of every company fact you used. student_items_used: the ids (S..) you used.
{{feedback}}

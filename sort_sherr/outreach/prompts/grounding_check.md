You are a strict fact-checker. Below are draft cold emails, the facts we know about the company
(ids F..) and the student's resume items (ids S..). List EVERY factual statement in each email and
say whether it is backed.

For each claim return:
- variant: A or B
- claim: the statement, copied or closely paraphrased
- about: "company" (anything about the company, its product, launches, team, customers, numbers),
  "student" (anything about the student's projects, skills, experience, school, results), or
  "other" (greeting, the ask, availability, work setup, sign-off, opinions like "I'd love to")
- support: the id of the fact or student item that backs it, or null
- supported: true only if that fact or item really says it. Numbers, dates, names and tools must match.
  A claim that goes beyond the source (adds a number, a customer, a result, a tool) is NOT supported.
"other" claims are always supported (use support null).

COMPANY FACTS
{{facts}}

STUDENT ITEMS
{{student}}

THE ASK GIVEN TO THE WRITER (statements matching this count as "other")
{{ask}}

EMAILS
{{emails}}

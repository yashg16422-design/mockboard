# Fact cards: how to author real ones

The board may only challenge a candidate's claim when a fact card backs it, so every card has to be something you have checked yourself against an official source. The three cards in each topic file right now are **synthetic test cards**. They talk about a made-up country called Exampleland, they start with "TEST CARD:", they carry `"synthetic_example": true` and `"source_url": null`, and they exist only so that the loader and the tests have something to read. Delete them, or leave them alongside your real cards for tests, but never present them in a demo as facts.

## What files exist

- `schema.json` is the JSON Schema for one topic file. It mirrors the `FactCard` model in `src/mockboard/brain/schemas.py`, and `src/mockboard/brain/cards.py` checks every topic file against both. If someone changes `FactCard`, this schema must be updated to match.
- `tourism.json` and `internal_security.json` are the two topic files. The `topic` field inside each file must equal the file name.

## The rules for a real card

1. **One checkable fact per card.** A card says one thing that can be right or wrong, such as a number for a year. If a sentence in the source contains two numbers, make two cards.
2. **Always record the number, the unit and the year.** Put the number in `value`, the unit in `unit`, and the year the figure refers to in `year`. The deterministic numeric check compares the candidate's claim against exactly these three fields, so a card without them can never support a fact challenge.
3. **Write the unit the way the numeric checker understands it.** Use `percent` for shares, `percentage_points` for differences between two percentages, `crore inr` or `lakh inr` for money in rupees, `lakh visitors` or `million visitors` for counts of people, and `year` for dates. The scale word (lakh, crore, million) goes in `unit`, not in `value`. For example, 1.5 crore rupees is `"value": 1.5, "unit": "crore inr"`.
4. **Copy the source exactly.** `source_title` is the document's title and edition (for example the title of the publication, the year of the edition, and the table or page number). `source_url` is the address of the page or PDF where you read it. Both are required on every real card; the loader refuses a real card without them.
5. **Record when you checked it.** Put the date you read the source in `as_of`, written as `YYYY-MM-DD`.
6. **Add aliases for how candidates actually talk.** List a few other phrasings, in English and Hinglish, that mean the same fact. These help the librarian match a spoken claim to the card.
7. **Set `kind` correctly.** Use `numeric` for quantities, `date` for "in which year" facts, and `qualitative` for checkable facts with no number. The orchestrator's ruling 1 says that only numeric and date claims can be challenged live, so qualitative cards only show up in the report.
8. **Never write a number from memory, and never guess a URL.** If you cannot find the figure in the official document, leave it out.

## Where to get the facts

Use official government publications. Search for them by name on the issuing ministry's own website, open the actual document, and copy the figure and the address from there.

- **Tourism:** the Ministry of Tourism's annual *India Tourism Statistics* publication (and its "at a Glance" summary). It covers foreign tourist arrivals, domestic tourist visits, foreign exchange earnings and similar figures.
- **Internal security:** the Ministry of Home Affairs (MHA) *Annual Report*. It has chapters on left-wing extremism, the North East, Jammu and Kashmir, border management and policing.
- **Either topic:** Press Information Bureau (PIB) releases, which often quote a minister's answer in Parliament with exact figures. Prefer the underlying report when the release cites one.

## A worked template

The example below shows the shape of a real card. Every value in angle brackets is a placeholder for you to fill in from the source; it is not a real figure.

```json
{
  "id": "tourism_fta_<year>",
  "statement": "India received <number> <unit> foreign tourist arrivals in <year>.",
  "aliases": ["foreign tourist arrivals <year>", "FTAs in <year>"],
  "kind": "numeric",
  "value": <number>,
  "unit": "<unit, e.g. million visitors>",
  "year": <year>,
  "source_title": "<publication title>, <edition>, <table or page>",
  "source_url": "<the exact address where you read it>",
  "as_of": "<YYYY-MM-DD you checked it>",
  "synthetic_example": false
}
```

## How to check your file

On your laptop (WSL), in the `mockboard` folder, run the loader. It prints the number of cards and the card-set version, and it raises a clear error if a card breaks the schema, a real card has no source, or two cards share an id.

```bash
PYTHONPATH=src python -c "from pathlib import Path; from mockboard.brain.cards import load_cards; cs = load_cards(Path('data/cards'), ['tourism', 'internal_security']); print(len(cs.cards), 'cards, version', cs.version, ', all synthetic:', cs.all_synthetic)"
```

The plan aims for 40 to 60 cards per topic. Editing any card changes the card-set version, which automatically invalidates cached librarian verdicts.

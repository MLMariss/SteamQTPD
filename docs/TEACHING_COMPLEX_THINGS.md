# Teaching Complex Things Easily
### Learnings from GMTK: "Why Are Strategy Game Tutorials So Bad?"

Core premise: complex games (RTS, grand strategy, city builders) tend to frontload teaching, which overwhelms new players before they're invested. Simpler genres solved this by spreading learning out, making it hands on, and leaning on familiar patterns. These are the transferable techniques.

---

## 1. Split the tutorial across a single playthrough

**Problem:** Dumping all lessons at the start exceeds a player's willingness to learn before they're invested in the outcome.

**Solution:** Introduce mechanics gradually, right when they become relevant, instead of all at once.

**Key principle: "Inverted pyramid of decision making"** (Bruce Shelley, Civilization)
Start the player with very few decisions (turn 1: where to settle). Complexity grows naturally as the game world grows, so teaching can grow with it.

| Approach | Example |
|---|---|
| Gate mechanics behind progression | Frostpunk: each new system (gathering, generator, etc.) gets its own mini tutorial only once unlocked |
| Reveal UI gradually | Mini Metro: interface elements appear only as they become needed |
| Make UI a purchasable/earnable upgrade | Animal Crossing: New Horizons, the tool wheel is bought later, not given upfront |

**Upside:** Player plays the "real game" almost immediately; lessons land when they're actually useful; matches natural rise in investment.

**Downside:** Only works if the game's systems can be meaningfully staged. Many complex games are built assuming everything is online from turn one, so this requires a design decision made early in development, not bolted on later.

---

## 2. Split the tutorial across multiple playthroughs

**Problem:** Some games can't stage complexity within one campaign. Also relevant because strategy/city-builder games are usually replayed many times, not played once like a story game.

**Solution:** Treat the game like a fighting game: separate lessons into brackets (e.g. basics, advanced, strategy) and let the player leave the tutorial to actually play after each bracket, returning later for more.

**Example:** Mortal Kombat 11 segmented its audience (couch players, dabblers, connoisseurs, online PvPers) and matched tutorial depth to segment.

**Alternative version, across full playthroughs:** Ship a stripped down version of the game first, then reintroduce systems later (expansion packs, or an "easy mode" that removes systems rather than just weakening AI).

**Example:** Civilization V launched simpler than prior entries; expansions (Gods & Kings, Brave New World) reintroduced systems like religion and espionage once players already knew the basics.

**Upside:** Matches how these games are actually played (repeatedly); avoids re-teaching things every session.

**Downside:** Needs to be designed this way from the start. Not easy to retrofit, and commercially tied to expansion models if used that way.

---

## 3. Make learning hands-on (kinaesthetic learning)

**Definition: Kinaesthetic learning** — deep learning that occurs through physically doing a task, not just reading or watching.

**Problem:** "Click here" arrow prompts get the player to act, but don't require actual thought. As Asher Vollmer (Threes) put it: "As far as the game is concerned, I have advanced. As far as my brain is concerned, I've learned nothing."

**Solution:** Turn tutorial steps into small, real puzzles or open-ended tasks instead of literal click-through instructions.

**Examples:**
- Threes: instead of "swipe left twice," the game frames it as a small puzzle to solve
- Planet Zoo: shows one welfare fix, then asks the player to fix the rest of the zoo unaided
- Offworld Trading Company: gives objectives and lets the player find the solution themselves

**Upside:** Feels like playing the game from the start, not doing homework; engages actual problem solving.

**Downside:** Strategy games have slow feedback loops. A mistake in resource balance might not show consequences for hours, which weakens the "learn by doing" effect. Short, repeatable campaigns teach faster than long ones.

**Partial fixes for slow feedback:**
- Speed-run/quick-game modes reframed as training tools
- Advisor characters who flag mistakes in the moment (e.g. Offworld warning about underpriced sales)

---

## 4. Explain "why," not just "how"

**Problem:** It's easy to teach which button does what. It's much harder to teach when or why to use it, this is the actual strategic skill.

**Solution:** Advisor characters that give recommendations and warnings in context, plus faster feedback cycles so players can observe consequences of choices themselves.

**Upside:** Closes the gap between mechanical knowledge and strategic understanding.

**Downside:** Requires either scripted advisor logic or a faster/compressed version of the game to generate feedback quickly, both are extra design and engineering work.

---

## 5. Use affordances (lean on what players already know)

**Definition: Affordance** — a design element that suggests its own use because it matches something familiar from the real world or other software (e.g. spikes = danger, a key = unlocks something).

**Problem:** Complex UIs are one of the most overwhelming parts of these games for new players.

**Solution:** Borrow visual and interaction language from things players already understand, real-world logic, historical knowledge, or everyday apps.

**Examples:**
- Civilization leans on general historical knowledge
- Reigns copies the swipe-left/swipe-right gesture from dating apps
- Disco Elysium's dialogue UI is modeled on a social media feed
- Planet Zoo reuses patterns from Google Maps (location pin) and Google Sheets (filter funnel icon), and color coding (red = bad, green = good)

**Failure case:** Total War: Troy used an hourglass icon for "end turn." Players associated hourglasses with loading, not ending a turn (drawing on old Windows loading cursors, called "throbbers"). One playtester spent 40 minutes stuck on turn one. Fixed by swapping to an arrow icon before launch.

**Two takeaways stated directly in the video:**
1. Don't assume your audience has played other similar games.
2. Playtest your tutorials, a lot.

**Upside:** When done well, can remove the need for an explicit tutorial entirely.

**Downside:** Relies on correctly guessing what's actually familiar to your specific audience. Assumptions can be wrong (as with the hourglass), so this only works reliably with playtesting.

---

## 6. Other supporting techniques

| Technique | Problem it solves | How |
|---|---|---|
| Show, don't tell | Walls of text are hard to get through | Use short animations/visuals instead of long text explanations (e.g. Into the Breach's attack preview animations) |
| Keep text tight | Jargon and inconsistent language slow comprehension | Cut words, keep language consistent, avoid jargon |
| Avoid decorative-only voice acting | Wastes attention on flavor while real info goes unspoken/unread | Prioritize voicing or highlighting the functionally important text |
| Reference tools on demand | Players get stuck and have to leave the game to Google an answer | In-game tooltips, an encyclopedia/glossary, ability to rewind or replay tutorial segments |
| Multiple learning paths | Not all players learn the same way (visual, kinaesthetic, reading, etc.) | Offer more than one route, e.g. Offworld's scripted walkthrough plus separate practice challenges; Total War's separate tracks for newcomers vs returning players |

---

## Summary: three underlying principles

1. **Pace teaching to investment.** Spread lessons across time (within a campaign, or across multiple playthroughs) so players learn once they care, not before.
2. **Teach by doing, not by pointing.** Kinaesthetic, puzzle-like tasks build real understanding; literal click-through prompts don't. Fast feedback strengthens this further.
3. **Borrow familiarity.** Use affordances from the real world and other software so fewer things need explicit explanation, and playtest to confirm the assumptions actually hold.

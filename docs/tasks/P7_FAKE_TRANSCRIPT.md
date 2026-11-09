# P7: the full fake transcripts, for the reviewer to read

Generated at the P7 commit with `python -m mockboard.cli --fake` (offline fake LLM, synthetic cards,
no real call). Read these as a candidate would hear them: the ritual, five members in order, a spoken
hand-off at each change, each question after the last answer, a request handled, one close. The
`Exampleland` words are the fake run's synthetic test card, which only `--fake` ever shows.

Board-line md5 (`grep '^\['`) of the first one: `e748940f8faa2863b5891d3c71532a67` (was
`826ef550999cc34d9018806dce1b665e` for the old board). Debate is unchanged: `f2a23f33b90df75103f505a2696e6944`.
The whole golden file, with the report, is `tests/golden/upsc_cli_fake.txt`.

## 1. `examples/fake_session.txt` (the golden: `make cli-fake`)

What it shows: a fact challenge (turn 2) and the acknowledgement after a conceded error, a repeat
(`please repeat`, free, no answer spent), a topic change that never comes back (chess), an honest
"I don't know" that is reassured, the situation question, and the close after the 14th answer.

```
Agenda: nova (11 questions written by the planner, 0 replaced by templates, 4 ms)
[Chairperson Sharma] Namaste. Please be seated.
[Chairperson Sharma] Kanpur Nagar se hone aur Electronics padhne ke baad, aapko civil services kyun sahi raasta laga?
You: Kanpur mein maine dekha ki small electronics repair shops bahut hain, lekin skilled training kam hai. Main ITI ke saath tie-up karke repair skilling badhaunga.
[Chairperson Sharma] (probe, R8) Ji. Agar aap khud is sthiti mein hote, to pehla qadam kya uthate, aur kyun?
You: Exampleland mein tourism GDP ka 30 percent hai in 2023, isliye tourism hi growth ka main engine hai and jobs bhi wahi deta hai.
[Chairperson Sharma] Dhanyavaad. Shrimati Menon, please.
[Shrimati Menon] (move_on, D-HANDOFF) Shukriya. Namaste. Kanpur Nagar mein rehkar aapne logon ki kaunsi dikkat kareeb se dekhi, aur use door karne ke liye aap kya karenge?
You: Mujhe lagta hai tourism se local logon ko rozgaar milta hai. Lekin environment par bhi asar padta hai, so carrying capacity rules zaroori hain.
[Shrimati Menon] (fact_challenge, R4) Aapne 30 percent kaha. Exampleland test data ke 2023 ke figure ke hisaab se yeh 12 percent hai. Aapka figure kahan se hai?
You: Haan, shayad mera figure galat tha. Main card wala data check karunga, lekin mera point ki tourism important hai abhi bhi valid hai.
[Shrimati Menon] Theek hai, yeh maanna achhi baat hai.
[Shrimati Menon] (probe, R8) Achha. Is par aapko sabse badi dikkat kahan dikhti hai?
You: Homestays aur local guides ko train karna chahiye. Saath hi waste management par cess lagana chahiye.
[Chairperson Sharma] Shukriya. Ab Doctor Iyer ki baari hai.
[Doctor Iyer] (move_on, D-HANDOFF) Aabhaar. Electronics padhte hue aapne kaunsa ek vichar seekha jo aam logon ki zindagi se jud sakta hai, aur kaise?
You: Electronics padhne se mujhe system ki tarah sochna aaya, har cheez ke input aur output hote hain.
[Doctor Iyer] (counter, R6) Samajh mein aata hai. Lekin doosra paksh kehta hai ki isse nuksaan bhi ho sakta hai. Aap use kaise tolenge?
You: please repeat
[Doctor Iyer] Zaroor, sawal phir se suniye.
[Doctor Iyer] (repeat_question, D-REPEAT) Lekin doosra paksh kehta hai ki isse nuksaan bhi ho sakta hai. Aap use kaise tolenge?
You: Mujhe lagta hai ki technology se sarkari daftaron mein kaam tez ho sakta hai, lekin logon ki training bhi zaroori hai.
[Doctor Iyer] (pressure, R7) Aapki baat mein dum hai. Koi ek thos udaharan dijiye, jisse yeh aur saaf ho.
You: Digital system sab jagah kaam nahi karta, isliye pehle kuch gaon mein try karna chahiye.
[Chairperson Sharma] Dhanyavaad. Professor Sen, aap aage badhaiye.
[Professor Sen] (move_on, D-HANDOFF) Aabhaar. Chess ne aapke sochne ki aadat ko kaise badla, aur woh aapke kaam mein kaise dikhegi?
You: Mera hobby chess hai, usse maine seekha ki jaldbaazi mein faisla nahi lena chahiye.
[Professor Sen] (counter, R6) Ji. Is soch ke khilaaf sabse vazandar baat kya ho sakti hai, aur uska aap kya karenge?
You: chess ke alawa kisi aur topic pe baat karte hain
[Professor Sen] Zaroor, koi baat nahi.
[Chairperson Sharma] Dhanyavaad. Shri Rathore, please.
[Shri Rathore] (move_on, D-HANDOFF) Aabhaar. Haal ke samay mein aapne kis rashtriya mudde ko dhyan se follow kiya hai, aur kyun?
You: Public administration mujhe isliye pasand hai kyunki usme niti aur zameeni kaam dono hain.
[Shri Rathore] (counter, R6) Samajh mein aata hai. Agar yeh tareeka na chale, to aapka doosra raasta kya hoga?
You: Mujhe exact pata nahi hai, but I think local logon se baat karke pehle samasya samajhni chahiye.
[Shri Rathore] (reassure, R3) Koi baat nahi. Chaliye ise aasaan karte hain: is par aapka pehla khayal kya aata hai?
You: Pehle rahat samagri ki list banakar sabse zaroorat mand logon tak pahunchaunga, aur gaon ke pradhan se baat karunga.
[Shri Rathore] (next_question, D-SEED) Theek hai. Maan lijiye aap DM hain aur ek factory ke theke ke mazdoor hadtaal par hain. Dono pakshon se baat karke aap kaise aage badhenge?
You: Main dono pakshon se baat karunga, lekin kaam rokne ka asar bhi dekhunga.
[Chairperson Sharma] (move_on, D-CLOSE) Thank you, that will be all.
```

Per turn, from the trace (`answered in` is the segment the candidate was answering; `line asked in`
is where the board's next question sits, which moves at a hand-off):

| turn | answered in | line asked in | who speaks | route | rule | counted | request |
|---|---|---|---|---|---|---|---|
| t0 | form_hr | form_hr | Chairperson Sharma | next_question | R8 | yes | None |
| t1 | form_hr | home_district | Shrimati Menon | handoff | D-HANDOFF | yes | None |
| t2 | home_district | home_district | Shrimati Menon | next_question | R4 | yes | None |
| t3 | home_district | home_district | Shrimati Menon | next_question | R8 | yes | None |
| t4 | home_district | degree | Doctor Iyer | handoff | D-HANDOFF | yes | None |
| t5 | degree | degree | Doctor Iyer | next_question | R6 | yes | None |
| t6 | degree | degree | Doctor Iyer | handle_request | D-REPEAT | free | repeat |
| t7 | degree | degree | Doctor Iyer | next_question | R7 | yes | None |
| t8 | degree | interests | Professor Sen | handoff | D-HANDOFF | yes | None |
| t9 | interests | interests | Professor Sen | next_question | R6 | yes | None |
| t10 | interests | current_affairs | Shri Rathore | handoff | D-HANDOFF | yes | change_topic |
| t11 | current_affairs | current_affairs | Shri Rathore | next_question | R6 | yes | None |
| t12 | current_affairs | current_affairs | Shri Rathore | next_question | R3 | yes | None |
| t13 | current_affairs | current_affairs | Shri Rathore | next_question | D-SEED | yes | None |
| t14 | current_affairs | current_affairs | Chairperson Sharma | close | D-CLOSE | yes | None |

## 2. `examples/fake_panel.txt` (the topic-change script)

"tourism ke alawa kisi aur topic pe baat karte hain" is acknowledged by the member who heard it, the
Chairperson hands over, and tourism is not said again. Also a repeat, `Next question please` (a skip)
and an honest unknown.

```
Agenda: nova (11 questions written by the planner, 0 replaced by templates, 2 ms)
[Chairperson Sharma] Namaste. Please be seated.
[Chairperson Sharma] Kanpur Nagar se hone aur Electronics padhne ke baad, aapko civil services kyun sahi raasta laga?
You: Kanpur mein maine dekha ki small electronics repair shops bahut hain, lekin skilled training kam hai. Main ITI ke saath tie-up karke repair skilling badhaunga.
[Chairperson Sharma] (probe, R8) Ji. Agar aap khud is sthiti mein hote, to pehla qadam kya uthate, aur kyun?
You: Mujhe civil services isliye pasand hai kyunki main logon ke liye seedha kuch badalna chahta hoon.
[Chairperson Sharma] Dhanyavaad. Shrimati Menon, please.
[Shrimati Menon] (move_on, D-HANDOFF) Shukriya. Namaste. Kanpur Nagar mein rehkar aapne logon ki kaunsi dikkat kareeb se dekhi, aur use door karne ke liye aap kya karenge?
You: Mujhe lagta hai tourism se local logon ko rozgaar milta hai. Exampleland mein tourism GDP ka 30 percent hai in 2023, isliye tourism hi growth ka main engine hai.
[Shrimati Menon] (counter, R6) Samajh mein aata hai. Agar yeh tareeka na chale, to aapka doosra raasta kya hoga?
You: tourism ke alawa kisi aur topic pe baat karte hain
[Shrimati Menon] Zaroor, koi baat nahi.
[Chairperson Sharma] Shukriya. Ab Doctor Iyer ki baari hai.
[Doctor Iyer] (move_on, D-HANDOFF) Ji, dhanyavaad. Electronics padhte hue aapne kaunsa ek vichar seekha jo aam logon ki zindagi se jud sakta hai, aur kaise?
You: Electronics padhne se mujhe system ki tarah sochna aaya, har cheez ke input aur output hote hain.
[Doctor Iyer] (counter, R6) Theek hai. Kya is soch ki koi keemat bhi hai, jise aap nazar-andaaz nahi karenge?
You: please repeat
[Doctor Iyer] Zaroor, sawal phir se suniye.
[Doctor Iyer] (repeat_question, D-REPEAT) Kya is soch ki koi keemat bhi hai, jise aap nazar-andaaz nahi karenge?
You: Mujhe lagta hai ki technology se sarkari daftaron mein kaam tez ho sakta hai, lekin logon ki training bhi zaroori hai.
[Doctor Iyer] (pressure, R7) Achha. Is baat ke samarthan mein aap kaunsa ek kaaran sabse pehle rakhenge?
You: Digital system sab jagah kaam nahi karta, isliye pehle kuch gaon mein try karna chahiye.
[Chairperson Sharma] Dhanyavaad. Professor Sen, aap aage badhaiye.
[Professor Sen] (move_on, D-HANDOFF) Ji, shukriya. Chess ne aapke sochne ki aadat ko kaise badla, aur woh aapke kaam mein kaise dikhegi?
You: Mera hobby chess hai, usse maine seekha ki jaldbaazi mein faisla nahi lena chahiye.
[Professor Sen] (counter, R6) Achha. Maan lijiye koi kahe ki yeh tareeka sabke liye theek nahi, to aapka jawab kya hoga?
You: Cycling se mujhe stamina aur dhairya mila, aur lambe raste ke liye planning karna aaya.
[Professor Sen] (steelman, R6) Achha. Ab doosre paksh ki jagah baithiye: unka sabse mazboot tark kya hoga?
You: Next question please
[Professor Sen] Koi baat nahi, ise chhod dete hain.
[Chairperson Sharma] Dhanyavaad. Shri Rathore, please.
[Shri Rathore] (move_on, D-HANDOFF) Aabhaar. Haal ke samay mein aapne kis rashtriya mudde ko dhyan se follow kiya hai, aur kyun?
You: Public administration mujhe isliye pasand hai kyunki usme niti aur zameeni kaam dono hain.
[Shri Rathore] (counter, R6) Ji. Is soch ke khilaaf sabse vazandar baat kya ho sakti hai, aur uska aap kya karenge?
You: Mujhe exact pata nahi hai, but I think local logon se baat karke pehle samasya samajhni chahiye.
[Shri Rathore] (reassure, R3) Jaldi nahi hai. Ek aasaan shuruaat kijiye: is vishay se aapka pehla vaasta kab pada?
You: Pehle rahat samagri ki list banakar sabse zaroorat mand logon tak pahunchaunga, aur gaon ke pradhan se baat karunga.
[Shri Rathore] (next_question, D-SEED) Achha. Maan lijiye aap DM hain aur ek factory ke theke ke mazdoor hadtaal par hain. Dono pakshon se baat karke aap kaise aage badhenge?
You: Main dono pakshon se baat karunga, lekin kaam rokne ka asar bhi dekhunga.
[Chairperson Sharma] (move_on, D-CLOSE) Thank you, that will be all.
```

| turn | answered in | line asked in | who speaks | route | rule | counted | request |
|---|---|---|---|---|---|---|---|
| t0 | form_hr | form_hr | Chairperson Sharma | next_question | R8 | yes | None |
| t1 | form_hr | home_district | Shrimati Menon | handoff | D-HANDOFF | yes | None |
| t2 | home_district | home_district | Shrimati Menon | next_question | R6 | yes | None |
| t3 | home_district | degree | Doctor Iyer | handoff | D-HANDOFF | yes | change_topic |
| t4 | degree | degree | Doctor Iyer | next_question | R6 | yes | None |
| t5 | degree | degree | Doctor Iyer | handle_request | D-REPEAT | free | repeat |
| t6 | degree | degree | Doctor Iyer | next_question | R7 | yes | None |
| t7 | degree | interests | Professor Sen | handoff | D-HANDOFF | yes | None |
| t8 | interests | interests | Professor Sen | next_question | R6 | yes | None |
| t9 | interests | interests | Professor Sen | next_question | R6 | yes | None |
| t10 | interests | current_affairs | Shri Rathore | handoff | D-HANDOFF | yes | skip |
| t11 | current_affairs | current_affairs | Shri Rathore | next_question | R6 | yes | None |
| t12 | current_affairs | current_affairs | Shri Rathore | next_question | R3 | yes | None |
| t13 | current_affairs | current_affairs | Shri Rathore | next_question | D-SEED | yes | None |
| t14 | current_affairs | current_affairs | Chairperson Sharma | close | D-CLOSE | yes | None |

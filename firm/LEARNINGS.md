# Learnings

- Short-term option buying on SENSEX/NIFTY lost in every form tested: index direction can be guessed slightly better
  than chance, but premiums move first and decay + costs exceed the edge.
- High live win rates over a few days are not evidence: one breakout opens several trades; count independent events.
- Check look-ahead: a filter known only at the minute's close must enter at that close.
- Selling time decay (defined-risk condor) is the only family positive over 3.4 years; adjusting it destroys the edge.

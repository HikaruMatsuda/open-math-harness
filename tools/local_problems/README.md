# Local frozen statements

Frozen Lean files for the local `lean` service (`tools/local_chat.py`). Each
`<problem>.lean` is the statement a candidate proof is judged against: the file
begins with the preamble a candidate must copy, and its theorem (named after the
file) has the proof `sorry`.

These are written for this local composition. They are **not** the official
PutnamBench set the deployed image carries under `/opt/nucleus/problems`; a
problem id here means only the file of that name in this directory. Replace or
extend them by adding `<problem>.lean` files (letters, digits and `_`), and
restart the composition.

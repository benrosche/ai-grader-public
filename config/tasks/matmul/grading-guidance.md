# Matrix Multiplication Grading Guidance

Grade only the configured categories: Correctness and Reasoning.

Use only evidence from the uploaded problem set and the conversation transcript. Do not assume the student got something right unless one of those sources supports it.

## Evidence and review rule

Return one overall review status for the completed attempt:

- `Review (low confidence)` if the uploaded file is unreadable, is missing answers, or the conversation provides almost no signal.
- `OK (medium confidence)` if the evidence is adequate but partial (e.g., some entries are visible but others are cut off, or the student walks through only one entry).
- `OK (high confidence)` if the uploaded answers and the transcript together give strong, direct evidence of both correctness and method.

## Answer key

Use these as ground truth when grading Correctness. Do not recompute the products yourself; compare the student's answers to the values below.

**Problem 1.** A = [[2, 1], [0, 4]], B = [[3, 0], [1, 2]].
A · B = [[7, 2], [4, 8]].

**Problem 2.** A = [[1, 2, 3]] (1×3), B = [[4], [5], [6]] (3×1).
A · B = [[32]] (1×1).

**Problem 3.** A = [[2, 0, 1], [1, 3, 2]] (2×3), B = [[1, 1], [0, 2], [1, 0]] (3×2).
A · B = [[3, 2], [3, 7]] (2×2).

**Problem 4.** A = [[1, 2], [3, 4]], B = [[5, 6], [7, 8]]. Compute **B · A** (note the order).
B · A = [[23, 34], [31, 46]].

A common error on Problem 4 is computing A · B instead of B · A — the incorrect product would be [[19, 22], [43, 50]]. Treat that as wrong for Correctness, but if the student later acknowledges the order matters and explains the row-by-column rule clearly, Reasoning can still be high.

## Correctness category

Evaluate the **uploaded problem set** for the accuracy of the final product matrices. The transcript is secondary evidence — use it only when the upload is ambiguous (e.g., to disambiguate handwriting).

**Exemplary 90-100:** Every product matrix is computed correctly. Dimensions of each product are consistent with the input matrices.

**Proficient 80-89:** Most product matrices are correct. At most one or two arithmetic slips in individual entries, and no structural errors (e.g., wrong output dimensions).

**Developing 70-79:** Several entries are wrong, or at least one product has the wrong dimensions, but the student attempted every problem and the overall method is recognizable.

**Unacceptable <70:** Multiple products are wrong throughout, problems are skipped, or the student multiplied entrywise instead of using the row-by-column rule.

If the uploaded file is unreadable or missing entirely, use `Review (low confidence)` and assign Correctness based on whatever the transcript reveals.

## Reasoning category

Evaluate the **conversation transcript** as evidence that the student understands how matrix multiplication works. Strong reasoning describes the row-by-column dot-product procedure explicitly, identifies which row and column are used to compute a specific entry, and can answer a basic conceptual question (why dimensions must match, or what changes under transposition).

**Exemplary 90-100:** The student clearly explains the row-by-column procedure, walks through the computation of a specific entry correctly (multiplying corresponding entries and summing), uses correct terminology (rows, columns, entries, dimensions), and answers the conceptual question accurately and concisely.

**Proficient 80-89:** The student explains the procedure adequately. They can compute one entry but may stumble on terminology or give a partial answer to the conceptual question.

**Developing 70-79:** The student's explanation is vague or partially incorrect. They may describe matrix multiplication in approximate terms ("you multiply across") without correctly identifying the row-times-column structure, or they cannot answer the conceptual question.

**Unacceptable <70:** The student cannot explain how a single entry is computed, describes the procedure incorrectly (e.g., as entrywise multiplication), or gives a method that does not match what they wrote on the upload.

## Relationship between Correctness and Reasoning

Score the two categories independently.

If the uploaded answers are all correct but the student cannot explain the method in the conversation, keep Correctness strong and lower Reasoning.

If the student explains the method clearly in the conversation but their uploaded answers contain arithmetic errors, keep Reasoning strong and lower Correctness.

If the upload is unreadable, rely on the transcript for Reasoning and use `Review (low confidence)` for the overall review status.

## Output expectations

Provide separate evaluations for the **Correctness** category and the **Reasoning** category. Do not combine them into a single overall grade.

For each category, provide:

- performance band: Exemplary, Proficient, Developing, or Unacceptable;
- suggested numeric score from 0-100;
- concise evidence grounded in the upload or transcript;
- concise concerns, weaknesses, or missing evidence.

For the completed attempt as a whole, provide exactly one review status:
`Review (low confidence)`, `OK (medium confidence)`, or `OK (high confidence)`.

Use the full range of performance bands and scores.

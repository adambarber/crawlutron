# Golden corpus provenance and licences

This directory holds the CI-eligible subset of the reference corpora gathered for issue #1139, copied from
`/private/var/folders/nt/19jslzfn6pb7j4_8rwn0gn940000gn/T/opencode/corpus-1139/`. Every file is permissively
licensed and the total is under 100 KB so the battery stays fast and offline.

## html5lib-tests (MIT)

Source: https://github.com/html5lib/html5lib-tests, `tokenizer/` and `encoding/` trees.

| File | Licence | Provenance |
|---|---|---|
| encoding_test-yahoo-jp.dat | MIT | html5lib-tests `encoding/test-yahoo-jp.dat` |
| encoding_tests2.dat | MIT | html5lib-tests `encoding/encoding_tests2.dat` |
| tokenizer_escapeFlag.test | MIT | html5lib-tests `tokenizer/tokenizer_escapeFlag.test` |
| tokenizer_test1.test | MIT | html5lib-tests `tokenizer/tokenizer_test1.test` |
| tokenizer_unicodeCharsProblematic.test | MIT | html5lib-tests `tokenizer/tokenizer_unicodeCharsProblematic.test` |
| tokenizer_xmlViolation.test | MIT | html5lib-tests `tokenizer/tokenizer_xmlViolation.test` |

## pdfminer.six samples (MIT)

Source: https://github.com/pdfminer/pdfminer.six, `samples/` tree. `jo.pdf`, `simple2.pdf` and
`zen_of_python_corrupted.pdf` are already shipped under `packages/extract/tests/fixtures/` and are not
re-copied here.

| File | Licence | Provenance |
|---|---|---|
| simple1.pdf | MIT | pdfminer.six `samples/simple1.pdf` |
| simple3.pdf | MIT | pdfminer.six `samples/simple3.pdf` |
| sampleOneByteIdentityEncode.pdf | MIT | pdfminer.six `samples/sampleOneByteIdentityEncode.pdf` |
| test_pattern_colors.pdf | MIT | pdfminer.six `samples/test_pattern_colors.pdf` |

## Apache POI test-data OOXML subset (Apache-2.0)

Source: https://github.com/apache/poi, `test-data/document`, `spreadsheet`, `slideshow` trees.
Legacy binary containers (`.ppt`, `.doc`, `.xls`) are excluded; only OOXML is kept.

| File | Licence | Provenance |
|---|---|---|
| 1_NoIden.xlsx | Apache-2.0 | Apache POI `test-data/spreadsheet/1_NoIden.xlsx` |
| 123233_charts.xlsx | Apache-2.0 | Apache POI `test-data/spreadsheet/123233_charts.xlsx` |
| 49386-null_dates.pptx | Apache-2.0 | Apache POI `test-data/slideshow/49386-null_dates.pptx` |
| 51187.pptx | Apache-2.0 | Apache POI `test-data/slideshow/51187.pptx` |
| 52288.docx | Apache-2.0 | Apache POI `test-data/document/52288.docx` |
| 55733.docx | Apache-2.0 | Apache POI `test-data/document/55733.docx` |

## Size

`du -ck` over this directory is ~87 KB, under the 100 KB cap.

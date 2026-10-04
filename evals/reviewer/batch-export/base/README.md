# Batch export

The command copies a UTF-8 text file to a receiver database. Each input line is one record, and records retain their input order. An input file is stable during a job and its retries; its absolute path identifies the source. Batches contain two records.

Run `python courier.py source.txt receiver.sqlite3`. Repeating a command against the same receiver is supported. The receiver stores each request by source and starting offset. Repeating that request with the same payload acknowledges the existing delivery; a different payload for that request is rejected. A connection error does not establish whether a request committed.

Run the repository tests with `python -B checks.py -v`.

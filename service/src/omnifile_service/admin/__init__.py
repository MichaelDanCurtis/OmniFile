"""Administrator commands (`python -m omnifile_service.admin`).

The catalog's administrator runs these, as it runs the migrations: no app may
write what they write. The first one registers storage backends, so a
deployment never has to INSERT `catalog.backend` rows by hand::

    python -m omnifile_service.admin backend add azure --live ACCOUNT --archive ACCOUNT
    python -m omnifile_service.admin backend add azure --account ACCOUNT --worm
    python -m omnifile_service.admin backend add s3 --account ID --region REGION --buckets A,B
    python -m omnifile_service.admin backend list
    python -m omnifile_service.admin backend show NAME

The connection is `catalog/connect.py`'s, as the administrator (`db.py`): a
connection string in ``OMNIFILE_DATABASE_URL`` (with the administrator's Entra
token in ``PGPASSWORD``, as the migrations take it), or ``OMNIFILE_PG_HOST``,
``OMNIFILE_PG_DATABASE`` and ``OMNIFILE_PG_USER`` with Entra sign-in.

Each row is checked by the code the service builds it with (`backends.py`), and
`add` is idempotent: the same answers again change nothing.
"""

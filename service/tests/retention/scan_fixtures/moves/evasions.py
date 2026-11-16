# Fixture for delete_scan (never imported): archive moves that skip the
# check in each way the scan must catch, and one that doesn't skip it.
# ruff: noqa


async def split_across_helpers(blob, src):
    """The copy and the delete are in helpers; neither copies and deletes."""
    await _copy_it(blob, src)
    await _drop_it(src)


async def _copy_it(blob, src):
    await blob.start_copy_from_url(src)


async def _drop_it(src):
    await src.delete_blob()


async def by_staged_blocks(blob, src):
    await blob.stage_block_from_url("id", src)
    await src.delete_blob()


def by_s3_part_copy(s3, bucket, key):
    s3.upload_part_copy(Bucket=bucket, Key=key + ".archive", CopySource=key)
    s3.delete_object(Bucket=bucket, Key=key)


async def check_ignored(blob, src, head):
    await blob.start_copy_from_url(src)
    copy_mismatch(size=1, crc64nvme=None, md5=None, copy=head, read_back=False)
    await src.delete_blob()


async def check_assigned_never_read(blob, src, head):
    await blob.start_copy_from_url(src)
    why = copy_mismatch(size=1, crc64nvme=None, md5=None, copy=head, read_back=False)
    await src.delete_blob()


async def checked_properly(blob, src, head):
    await blob.start_copy_from_url(src)
    why = copy_mismatch(size=1, crc64nvme=None, md5=None, copy=head, read_back=False)
    if why is not None:
        raise RuntimeError(why)
    await src.delete_blob()


async def checked_in_a_helper(blob, src, head):
    await blob.start_copy_from_url(src)
    _verify(head)
    await src.delete_blob()


def _verify(head):
    if copy_mismatch(size=1, crc64nvme=None, md5=None, copy=head, read_back=False):
        raise RuntimeError("bad copy")

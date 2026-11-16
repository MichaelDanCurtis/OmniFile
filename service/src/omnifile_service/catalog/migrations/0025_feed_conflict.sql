-- The api's one feed write: the conflict event POST .../changes adds when an
-- edge's change loses (api/changes.py). Through this owner-run function the
-- api no longer needs INSERT on catalog.feed_event, so it can't forge an
-- upsert, rename or delete for another dataset's edges. Execution is granted
-- to the api alone, by privileges.py.
CREATE FUNCTION catalog.feed_conflict(p_edge_id bigint, p_file_id bigint, p_op_id text,
                                      p_reason text) RETURNS void
LANGUAGE sql AS $$
    INSERT INTO catalog.feed_event
        (dataset_id, edge_id, entity, entity_id, entity_version, op,
         origin_edge_id, origin_op_id, detail)
    SELECT f.dataset_id, p_edge_id, 'file', f.file_id, f.entity_version, 'conflict',
           p_edge_id, p_op_id, jsonb_build_object('op_id', p_op_id, 'reason', p_reason)
      FROM catalog.file f WHERE f.file_id = p_file_id
$$;

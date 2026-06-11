-- Per-loop source IP ("Node = IP"). The outbound UAC binds to this address
-- (-i/-mi) and the engine enforces one running loop per IP, so many source IPs
-- can each run their own loop concurrently. NULL/empty => the OS picks per
-- routing (the legacy single-IP behaviour from [sip] local_ip).
ALTER TABLE loop_campaigns ADD COLUMN local_ip VARCHAR(45);

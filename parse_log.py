"""
Parse replica set member's mongod.log. The member must have verbose logging
enabled like:

    db.adminCommand({
        setParameter: 1,
        logComponentVerbosity: {tlaPlusTrace: 1}
    })
"""
import datetime
import heapq
import re
import sys
from json import JSONDecodeError

from bson import json_util  # pip install pymongo

from repl_checker_dataclass import repl_checker_dataclass
from system_state import OplogEntry, CommitPoint, ServerState

# Match lines like:
# 2019-07-16T12:24:41.964-0400 I  TLA_PLUS_TRACE [replexec-0]
#   {"action": "BecomePrimaryByMagic", ...}
line_pat = re.compile(
    r'(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.\d{3}[+-]\d{4})'
    r'.+? TLA_PLUS \[(?P<threadName>[\w\-\d]+)] '
    r'(?P<json>{.*})')


def parse_log_timestamp(timestamp_str):
    return datetime.datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%S.%f%z')


@repl_checker_dataclass(order=True)
class LogLine:
    # Ordered so that a sequence of LogLines are sorted by timestamp.
    timestamp: datetime.datetime
    location: str
    line: str
    obj: dict


def merge_log_streams(streams):
    """Merge logs, sorting by timestamp."""

    def gen(stream):
        line_number = 0
        for line in stream:
            line_number += 1
            match = line_pat.match(line)
            if not match:
                continue

            timestamp = parse_log_timestamp(match.group('timestamp'))
            try:
                # json_util converts e.g. $numberLong to Python int.
                obj = json_util.loads(match.group('json'))
            except JSONDecodeError as exc:
                print(f"Invalid JSON in {stream.name}:{line_number}"
                      f" {exc.msg} in column {exc.colno}:\n"
                      f"{match.group('json')}")
                sys.exit(2)

            # Yield tuples
            yield LogLine(timestamp=timestamp,
                          location=f'{stream.name}:{line_number}',
                          line=line,
                          obj=obj)

    return heapq.merge(*map(gen, streams))


@repl_checker_dataclass
class LogEvent:
    timestamp: datetime.datetime
    """The server log timestamp."""
    location: str
    """File name and line number, like 'file.log:123'."""
    line: str
    """The text of the server log line"""
    action: str
    """The action (in TLA+ spec terms) the server is taking."""
    server_id: int
    """The server's id (0-indexed)."""
    term: int
    """The server's view of the term.
    
    NOTE: The implementation's term starts at -1, then increases to 1, then
    increments normally. We treat -1 as if it were 0.
    """
    state: ServerState
    """The server's replica set member state."""
    commitPoint: CommitPoint
    """The server's view of the commit point."""
    log: tuple
    """The server's oplog."""

{{ action }} server_id={{ server_id }} state={{ state.name }} term={{ term }}
    __pretty_template__ = """{{ location }} at {{ timestamp | mongo_dt }}
commit point: {{ commitPoint }}
log: {{ log | oplog }}"""


def parse_log_line(log_line, port_mapper, oplog_index_mapper):
    """Transform a LogLine into a LogEvent."""
    try:
        # Generic logging is in "trace", RaftMongo.tla-specific in "raft_mongo".
        trace = log_line.obj
        raft_mongo = trace['state']
        port = int(trace['host'].split(':')[1])

        # JSON oplog is a list of optimes with timestamp "ts" and term "t", like
        # [{ts: {$timestamp: {t: 123, i: 4}}, t: {"$numberLong" : "1" }}, ...].
        # Update timestamp -> index map, which we use below for CommitPoint.
        def generate_oplog_entries():
            for index, entry in enumerate(raft_mongo['log']):
                oplog_index_mapper.set_index(entry['ts'], index)
                yield OplogEntry(term=entry['t'])

        log = tuple(generate_oplog_entries())

        # What the implementation calls -1, the spec calls 0.
        def fixup_term(term):
            return 0 if term == -1 else term

        commitPoint = CommitPoint(
            term=fixup_term(raft_mongo['commitPoint']['t']),
            index=oplog_index_mapper.get_index(raft_mongo['commitPoint']['ts']))

        return LogEvent(timestamp=log_line.timestamp,
                        location=log_line.location,
                        line=log_line.line,
                        action=trace['action'],
                        server_id=port_mapper.get_server_id(port),
                        term=fixup_term(raft_mongo['term']),
                        state=ServerState[raft_mongo['serverState']],
                        commitPoint=commitPoint,
                        log=log)
    except Exception:
        print('Exception parsing line: {!r}'.format(log_line))
        raise

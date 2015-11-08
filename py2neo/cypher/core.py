#!/usr/bin/env python
# -*- encoding: utf-8 -*-

# Copyright 2011-2014, Nigel Small
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from collections import OrderedDict
import logging

from py2neo import Graphy, Bindable, Resource, Node, Relationship, Subgraph, Path, Finished
from py2neo.compat import integer, string, xstr, ustr
from py2neo.cypher.lang import cypher_escape
from py2neo.cypher.error.core import CypherError, TransactionError
from py2neo.packages.tart.tables import TextTable
from py2neo.util import is_collection, deprecated


__all__ = ["CypherEngine", "Transaction", "Result", "RecordStream",
           "Record", "RecordProducer"]


log = logging.getLogger("py2neo.cypher")


def first_node(x):
    if hasattr(x, "__nodes__"):
        try:
            return next(x.__nodes__())
        except StopIteration:
            raise ValueError("No such node: %r" % x)
    raise ValueError("No such node: %r" % x)


def last_node(x):
    if hasattr(x, "__nodes__"):
        nodes = list(x.__nodes__())
        if nodes:
            return nodes[-1]
    raise ValueError("No such node: %r" % x)


def presubstitute(statement, parameters):
    more = True
    presub_parameters = []
    while more:
        before, opener, key = statement.partition(u"«")
        if opener:
            key, closer, after = key.partition(u"»")
            try:
                value = parameters[key]
                presub_parameters.append(key)
            except KeyError:
                raise KeyError("Expected a presubstitution parameter named %r" % key)
            if isinstance(value, integer):
                value = ustr(value)
            elif isinstance(value, tuple) and all(map(lambda x: isinstance(x, integer), value)):
                value = u"%d..%d" % (value[0], value[-1])
            elif is_collection(value):
                value = ":".join(map(cypher_escape, value))
            else:
                value = cypher_escape(value)
            statement = before + value + after
        else:
            more = False
    parameters = {k:v for k,v in parameters.items() if k not in presub_parameters}
    return statement, parameters


class CypherEngine(Bindable):
    """ Service wrapper for all Cypher functionality, providing access
    to transactions as well as single statement execution and streaming.

    This class will usually be instantiated via a :class:`py2neo.Graph`
    object and will be made available through the
    :attr:`py2neo.Graph.cypher` attribute. Therefore, for single
    statement execution, simply use the :func:`execute` method::

        from py2neo import Graph
        graph = Graph()
        results = graph.cypher.execute("MATCH (n:Person) RETURN n")

    """

    error_class = CypherError

    __instances = {}

    def __new__(cls, transaction_uri):
        try:
            inst = cls.__instances[transaction_uri]
        except KeyError:
            inst = super(CypherEngine, cls).__new__(cls)
            inst.bind(transaction_uri)
            cls.__instances[transaction_uri] = inst
        return inst

    def post(self, statement, parameters=None, **kwparameters):
        """ Post a Cypher statement to this resource, optionally with
        parameters.

        :arg statement: A Cypher statement to execute.
        :arg parameters: A dictionary of parameters.
        :arg kwparameters: Extra parameters supplied by keyword.
        """
        tx = Transaction(self.uri)
        result = tx.execute(statement, parameters, **kwparameters)
        tx.post(commit=True)
        return result

    def run(self, statement, parameters=None, **kwparameters):
        """ Execute a single Cypher statement, ignoring any return value.

        :arg statement: A Cypher statement to execute.
        :arg parameters: A dictionary of parameters.
        """
        tx = Transaction(self.uri)
        tx.execute(statement, parameters, **kwparameters)
        tx.commit()

    def execute(self, statement, parameters=None, **kwparameters):
        """ Execute a single Cypher statement.

        :arg statement: A Cypher statement to execute.
        :arg parameters: A dictionary of parameters.
        :rtype: :class:`py2neo.cypher.Result`
        """
        tx = Transaction(self.uri)
        result = tx.execute(statement, parameters, **kwparameters)
        tx.commit()
        return result

    def evaluate(self, statement, parameters=None, **kwparameters):
        """ Execute a single Cypher statement and return the value from
        the first column of the first record returned.

        :arg statement: A Cypher statement to execute.
        :arg parameters: A dictionary of parameters.
        :return: Single return value or :const:`None`.
        """
        tx = Transaction(self.uri)
        result = tx.execute(statement, parameters, **kwparameters)
        tx.commit()
        return result.value()

    def stream(self, statement, parameters=None, **kwparameters):
        """ Execute the query and return a result iterator.

        :arg statement: A Cypher statement to execute.
        :arg parameters: A dictionary of parameters.
        :rtype: :class:`py2neo.cypher.RecordStream`
        """
        return RecordStream(self.graph, self.post(statement, parameters, **kwparameters))

    def begin(self):
        """ Begin a new transaction.

        :rtype: :class:`py2neo.cypher.Transaction`
        """
        return Transaction(self.uri)


class Transaction(object):
    """ A transaction is a transient resource that allows multiple Cypher
    statements to be executed within a single server transaction.
    """

    error_class = TransactionError

    def __init__(self, uri):
        log.info("begin")
        self.statements = []
        self.results = []
        self.__begin = Resource(uri)
        self.__begin_commit = Resource(uri + "/commit")
        self.__execute = None
        self.__commit = None
        self.__finished = False
        self.graph = self.__begin.graph

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def __assert_unfinished(self):
        if self.__finished:
            raise Finished(self)

    @property
    def _id(self):
        """ The internal server ID of this transaction, if available.
        """
        if self.__execute is None:
            return None
        else:
            return int(self.__execute.uri.path.segments[-1])

    @property
    def finished(self):
        """ Indicates whether or not this transaction has been completed or is
        still open.

        :return: :py:const:`True` if this transaction has finished,
                 :py:const:`False` otherwise
        """
        return self.__finished

    def execute(self, statement, parameters=None, **kwparameters):
        """ Add a statement to the current queue of statements to be
        executed.

        :arg statement: the statement to append
        :arg parameters: a dictionary of execution parameters
        """
        self.__assert_unfinished()

        s = ustr(statement)
        p = {}

        def add_parameters(params):
            if params:
                for k, v in dict(params).items():
                    if isinstance(v, (Node, Relationship)):
                        v = v._id
                    p[k] = v

        if hasattr(statement, "parameters"):
            add_parameters(statement.parameters)
        add_parameters(dict(parameters or {}, **kwparameters))

        s, p = presubstitute(s, p)

        # OrderedDict is used here to avoid statement/parameters ordering bug
        log.info("append %r %r", s, p)
        self.statements.append(OrderedDict([
            ("statement", s),
            ("parameters", p),
            ("resultDataContents", ["REST"]),
        ]))
        result = Result(self.graph)
        self.results.append(result)
        return result

    def create(self, *labels, **properties):
        return self.execute("CREATE (a:«l» {p}) "
                            "RETURN a",
                            l=labels, p=properties)

    def delete(self, node):
        pass

    def relate(self, *nodes, **properties):
        if len(nodes) != 3:
            raise ValueError("Start node, type and end node are required")
        start_node = last_node(nodes[0])
        end_node = first_node(nodes[2])
        relationship_type = nodes[1]
        return self.execute("MATCH (a) WHERE id(a)={x} "
                            "MATCH (b) WHERE id(b)={y} "
                            "CREATE UNIQUE (a)-[r:«t» {p}]->(b) "
                            "RETURN r",
                            x=start_node._id, y=end_node._id, t=relationship_type, p=properties)

    def post(self, commit=False, hydrate=False):
        self.__assert_unfinished()
        if commit:
            log.info("commit")
            resource = self.__commit or self.__begin_commit
            self.__finished = True
        else:
            log.info("process")
            resource = self.__execute or self.__begin
        rs = resource.post({"statements": self.statements})
        location = rs.location
        if location:
            self.__execute = Resource(location)
        j = rs.content
        rs.close()
        self.statements = []
        if "commit" in j:
            self.__commit = Resource(j["commit"])
        if "errors" in j:
            errors = j["errors"]
            if len(errors) >= 1:
                error = errors[0]
                raise self.error_class.hydrate(error)
        for j_result in j["results"]:
            result = self.results.pop(0)
            keys = j_result["columns"]
            producer = RecordProducer(keys)
            if hydrate:
                result.process(keys, [producer.produce(self.graph.hydrate(data["rest"]))
                                      for data in j_result["data"]])
            else:
                result.process(keys, [data["rest"] for data in j_result["data"]])
        #log.info("results %r", results)

    def process(self):
        """ Send all pending statements to the server for execution, leaving
        the transaction open for further statements. Along with
        :meth:`append <.Transaction.append>`, this method can be used to
        batch up a number of individual statements into a single HTTP request::

            from py2neo import Graph

            graph = Graph()
            statement = "MERGE (n:Person {name:{N}}) RETURN n"

            tx = graph.cypher.begin()

            def add_names(*names):
                for name in names:
                    tx.append(statement, {"N": name})
                tx.process()

            add_names("Homer", "Marge", "Bart", "Lisa", "Maggie")
            add_names("Peter", "Lois", "Chris", "Meg", "Stewie")

            tx.commit()

        """
        self.post(hydrate=True)

    def commit(self):
        """ Send all pending statements to the server for execution and commit
        the transaction.
        """
        self.post(commit=True, hydrate=True)

    def rollback(self):
        """ Rollback the current transaction.
        """
        self.__assert_unfinished()
        log.info("rollback")
        try:
            if self.__execute:
                self.__execute.delete()
        finally:
            self.__finished = True


class NotProcessedError(Exception):
    pass


class Result(object):
    """ A list of records returned from the execution of a Cypher statement.
    """

    def __init__(self, graph):
        self.graph = graph
        self._keys = []
        self._records = []
        self._processed = False

    def __repr__(self):
        return "<Result>"

    def __str__(self):
        return xstr(self.__unicode__())

    def __unicode__(self):
        self._assert_processed()
        out = ""
        if self._keys:
            table = TextTable([None] + self._keys, border=True)
            for i, record in enumerate(self._records):
                table.append([i + 1] + list(record))
            out = repr(table)
        return out

    def __len__(self):
        self._assert_processed()
        return len(self._records)

    def __getitem__(self, item):
        self._assert_processed()
        return self._records[item]

    def __iter__(self):
        self._assert_processed()
        return iter(self._records)

    def _assert_processed(self):
        if not self._processed:
            raise NotProcessedError("Result not yet processed")

    @property
    def processed(self):
        return self._processed

    def process(self, keys, records):
        self._keys = keys
        self._records = records
        self._processed = True

    def value(self):
        """ The first value from the first record of this result. If no records
        are available, :const:`None` is returned.
        """
        self._assert_processed()
        try:
            record = self[0]
        except IndexError:
            return None
        else:
            if len(record) == 0:
                return None
            elif len(record) == 1:
                return record[0]
            else:
                return record

    def to_subgraph(self):
        """ Convert a Result into a Subgraph.
        """
        self._assert_processed()
        entities = []
        for record in self._records:
            for value in record:
                if isinstance(value, (Node, Relationship, Path)):
                    entities.append(value)
        return Subgraph(*entities)


class RecordStream(object):
    """ An accessor for a sequence of records yielded by a streamed Cypher statement.

    ::

        for record in graph.cypher.stream("MATCH (n) RETURN n LIMIT 10")
            print record[0]

    Each record returned is cast into a :py:class:`namedtuple` with names
    derived from the resulting column names.

    .. note ::
        Results are available as returned from the server and are decoded
        incrementally. This means that there is no need to wait for the
        entire response to be received before processing can occur.
    """

    def __init__(self, graph, result):
        self.graph = graph
        self.__result = result
        self.__result_item = self.__result_iterator()
        self.columns = next(self.__result_item)
        log.info("stream %r", self.columns)

    def __result_iterator(self):
        columns = self.__result["columns"]
        producer = RecordProducer(columns)
        yield tuple(columns)
        for values in self.__result["data"]:
            yield producer.produce(self.graph.hydrate(values))

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.__result_item)

    def next(self):
        return self.__next__()

    def close(self):
        """ Close results and free resources.
        """
        pass


class Record(Graphy, object):
    """ A simple object containing values from a single row of a Cypher
    result. Each value can be retrieved by column position or name,
    supplied as either an index key or an attribute name.

    Consider the record below::

           | person                     | name
        ---+----------------------------+-------
         1 | (n1:Person {name:"Alice"}) | Alice

    If this record is named ``r``, the following expressions
    are equivalent and will return the value ``'Alice'``::

        r[1]
        r["name"]
        r.name

    """

    __producer__ = None

    def __init__(self, values):
        self.__values__ = tuple(values)
        columns = self.__producer__.columns
        for i, column in enumerate(columns):
            setattr(self, column, values[i])

    def __repr__(self):
        out = ""
        columns = self.__producer__.columns
        if columns:
            table = TextTable(columns, border=True)
            table.append([getattr(self, column) for column in columns])
            out = repr(table)
        return out

    def __eq__(self, other):
        try:
            return vars(self) == vars(other)
        except TypeError:
            return tuple(self) == tuple(other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __len__(self):
        return len(self.__values__)

    def __iter__(self):
        return iter(self.__values__)

    def __getitem__(self, item):
        if isinstance(item, integer):
            return self.__values__[item]
        elif isinstance(item, string):
            return getattr(self, item)
        else:
            raise LookupError(item)

    def __nodes__(self):
        """ Iterate through all nodes in this record.
        """
        for value in self.__values__:
            if isinstance(value, Node):
                yield value


class RecordProducer(object):

    def __init__(self, columns):
        self.__columns = tuple(column for column in columns if not column.startswith("_"))
        self.__len = len(self.__columns)
        dct = dict.fromkeys(self.__columns)
        dct["__producer__"] = self
        self.__type = type(xstr("Record"), (Record,), dct)

    def __repr__(self):
        return "RecordProducer(columns=%r)" % (self.__columns,)

    def __len__(self):
        return self.__len

    @property
    def columns(self):
        return self.__columns

    def produce(self, values):
        """ Produce a record from a set of values.
        """
        return self.__type(values)

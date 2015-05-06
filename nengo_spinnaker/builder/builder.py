"""SpiNNaker builder for Nengo models."""
import collections
import enum
from nengo.cache import NoDecoderCache
from nengo.utils.builder import objs_and_connections, remove_passthrough_nodes
from nengo.utils import numpy as npext
import numpy as np

from nengo_spinnaker.utils import collections as collections_ext
from nengo_spinnaker.utils.keyspaces import KeyspaceContainer


def get_seed(obj, rng):
    seed = rng.randint(npext.maxint)
    return (seed if getattr(obj, "seed", None) is None else obj.seed)


class Model(object):
    """Model which has been built specifically for simulation on SpiNNaker.

    Attributes
    ----------
    dt : float
        Simulation timestep in seconds.
    machine_timestep : int
        Real-time duration of a simulation timestep in microseconds.
    decoder_cache :
        Cache used to reduce the time spent solving for decoders.
    params : {object: build details, ...}
        Map of Nengo objects (Ensembles, Connections, etc.) to their built
        equivalents.
    seeds : {object: int, ...}
        Map of Nengo objects to the seeds used in their construction.
    keyspaces : {keyspace_name: keyspace}
        Map of keyspace names to the keyspace which they may use.
    objects_intermediates : {object: intermediate, ...}
        Map of objects to the intermediate objects which will simulate them on
        SpiNNaker.
    connections_signals : {connection: :py:`~.Signal`, ...}
        Map of connections to the signals that simulate them.
    """

    builders = collections_ext.registerabledict()
    """Builders for Nengo objects.

    Each object in the Nengo network is built by calling a builder function
    registered in this dictionary.  The builder function must be of the form:

        .. py:function:: builder(model, object)

    It is free to modify the model as required (including doing nothing to
    suppress SpiNNaker simulation of the object).
    """

    source_getters = collections_ext.registerabledict()
    """Functions to retrieve the specifications for the sources of signals.

    Before a connection is built an attempt is made to determine where the
    signal it represents on SpiNNaker will originate; a source getter is called
    to perform this task.  A source getter should resemble:

        .. py:function:: getter(model, connection)

    The returned item can be one of two things:
     * `None` will suppress simulation of the connection on SpiNNaker -- an
       example of this being useful is in optimising out connections from
       constant valued Nodes to ensembles or reusing an existing connection.
     * a :py:class:`~.spec` object which will be used to determine nature of
       the signal (in particular, the key and mask that it should use, whether
       it is latching or otherwise and the cost of the signal in terms of the
       frequency of packets across it).
    """

    sink_getters = collections_ext.registerabledict()
    """Functions to retrieve the specifications for the sinks of signals.

    A sink getter is analogous to a `source_getter`, but refers to the
    terminating end of a signal.
    """

    probe_builders = collections_ext.registerabledict()
    """Builder functions for probes.

    Probes can either require the modification of an existing object or the
    insertion of a new object into the model. A probe builder can be registered
    against the target of the probe and must be of the form:

        .. py:function:: probe_builder(model, probe)

    And is free the modify the model and existing objects as required.
    """

    def __init__(self, dt=0.001, machine_timestep=1000,
                 decoder_cache=NoDecoderCache(), keyspaces=None):
        self.dt = dt
        self.machine_timestep = machine_timestep
        self.decoder_cache = decoder_cache

        self.params = dict()
        self.seeds = dict()
        self.rng = None

        self.object_intermediates = dict()
        self.connections_signals = dict()

        if keyspaces is None:
            keyspaces = KeyspaceContainer()
        self.keyspaces = keyspaces

        # Internally used dictionaries to construct keyspace information
        self._obj_ids = collections.defaultdict(collections_ext.counter())
        self._obj_conn_ids = collections.defaultdict(
            lambda: collections.defaultdict(collections_ext.counter())
        )

        # Internally used dictionaries of build methods
        self._builders = collections_ext.mrolookupdict()

    def _get_object_and_connection_id(self, obj, connection):
        """Get a unique ID for the object and connection pair for use in
        building instances of the default Nengo keyspace.
        """
        # Get the object ID and then the connection ID
        obj_id = self._obj_ids[obj]
        conn_id = self._obj_conn_ids[obj][connection]
        return (obj_id, conn_id)

    def build(self, network, extra_builders={},
              extra_source_getters={}, extra_sink_getters={}):
        """Build a Network into this model.

        Parameters
        ----------
        network : :py:class:`~nengo.Network`
            Nengo network to build.  Passthrough Nodes will be removed.
        extra_builders : {type: fn, ...}
            Extra builder methods.
        extra_source_getters : {type: fn, ...}
            Extra source getter methods.
        extra_sink_getters : {type: fn, ...}
            Extra sink getter methods.
        """
        # Get the seed and random number generator

        self.seeds[network] = get_seed(network, np.random)
        self.rng = np.random.RandomState(self.seeds[network])

        # Get all objects and connections and remove all passthrough Nodes
        objs, conns = remove_passthrough_nodes(*objs_and_connections(network))

        # Get a clean set of builders
        self._builders = collections_ext.mrolookupdict()
        self._builders.update(self.builders)
        self._builders.update(extra_builders)

        # Build all objects
        for obj in objs:
            self.make_object(obj)

        # Get a clean set of getters
        self._source_getters = collections_ext.mrolookupdict()
        self._source_getters.update(self.source_getters)
        self._source_getters.update(extra_source_getters)

        self._sink_getters = collections_ext.mrolookupdict()
        self._sink_getters.update(self.sink_getters)
        self._sink_getters.update(extra_sink_getters)

        # Build all the connections
        for connection in conns:
            self.make_connection(connection)

    def make_object(self, obj):
        """Call an appropriate build function for the given object.
        """
        self.seeds[obj] = get_seed(obj, self.rng)
        self._builders[type(obj)](self, obj)

    def make_connection(self, conn):
        """Make a Connection and add a new signal to the Model.

        This method will build a connection and construct a new signal which
        will be included in the model.
        """
        self.seeds[conn] = get_seed(conn, self.rng)
        # TODO Build the connection!

        # Get the source and sink specification, then make the signal provided
        # that neither of specs is None.
        source = self._source_getters[type(conn.pre_obj)](self, conn)
        sink = self._sink_getters[type(conn.post_obj)](self, conn)

        if source is not None and sink is not None:
            assert conn not in self.connections_signals
            self.connections_signals[conn] = _make_signal(self, conn,
                                                          source, sink)


ObjectPort = collections.namedtuple("ObjectPort", "obj port")
"""Source or sink of a signal.

Parameters
----------
obj : intermediate object
    Intermediate representation of a Nengo object, or other object, which is
    the source or sink of a signal.
port : port
    Port that is the source or sink of a signal.
"""


class OutputPort(enum.Enum):
    """Indicate the intended transmitting part of an executable."""
    standard = 0
    """Standard, value-based, output port."""


class InputPort(enum.Enum):
    """Indicate the intended receiving part of an executable."""
    standard = 0
    """Standard, value-based, output port."""


class spec(collections.namedtuple("spec",
                                  "target, keyspace, weight, latching")):
    """Specification of a signal which can be returned by either a source or
    sink getter.

    Attributes
    ----------
    target : :py:class:`ObjectPort`
        Source or sink of a signal.

    The other attributes and arguments are as for :py:class:`~.Signal`.
    """
    def __new__(cls, target, keyspace=None, weight=0, latching=False):
        return super(spec, cls).__new__(cls, target, keyspace,
                                        weight, latching)


class Signal(
        collections.namedtuple("Signal",
                               "source, sinks, keyspace, weight, latching")):
    """Represents a stream of multicast packets across a SpiNNaker machine.

    Attributes
    ----------
    source : :py:class:`~.ObjectPort`
        Source object and port of signal.
    sinks : [:py:class:`.~ObjectPort`, ...]
        Sink objects and ports of the signal.
    keyspace : keyspace
        Keyspace used for packets representing the signal.
    weight : int
        Number of packets expected to represent the signal during a single
        timestep.
    latching : bool
        Indicates that the receiving buffer must *not* be reset every
        simulation timestep but must hold its value until it next receives a
        packet.
    """
    def __new__(cls, source, sinks, keyspace, weight=0, latching=False):
        # Ensure the sinks are a list
        if isinstance(sinks, collections.Iterable):
            sinks = list(sinks)
        else:
            sinks = [sinks]

        # Create the tuple
        return super(Signal, cls).__new__(cls, source, sinks, keyspace,
                                          weight, latching)


def _make_signal(model, connection, source_spec, sink_spec):
    """Create a Signal."""
    # Get the keyspace
    if source_spec.keyspace is None and sink_spec.keyspace is None:
        # Using the default keyspace, get the object and connection ID
        obj_id, conn_id = model._get_object_and_connection_id(
            connection.pre_obj, connection
        )

        # Create the keyspace from the default one provided by the model
        keyspace = model.keyspaces["nengo"](object=obj_id, connection=conn_id)
    elif source_spec.keyspace is not None and sink_spec.keyspace is None:
        # Use the keyspace required by the source
        keyspace = source_spec.keyspace
    elif source_spec.keyspace is None and sink_spec.keyspace is not None:
        # Use the keyspace required by the sink
        keyspace = sink_spec.keyspace
    else:
        # Collision between the keyspaces
        raise NotImplementedError("Cannot merge two keyspaces")

    # Get the weight
    weight = max((0 or source_spec.weight,
                  0 or sink_spec.weight,
                  getattr(connection.post_obj, "size_in", 0)))

    # Determine if the connection is latching - there should probably never be
    # a case where these requirements differ, but this may need revisiting.
    latching = source_spec.latching or sink_spec.latching

    # Create the signal
    return Signal(
        source_spec.target, sink_spec.target, keyspace, weight, latching
    )
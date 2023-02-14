from django.db.models.expressions import Col, ExpressionList
from django.db.models.fields import Field
from django.db.models.lookups import TupleExact, CompositeExact, CompositeIn
from django.db.models.query_utils import DeferredAttribute
from django.db.models.signals import class_prepared
from django.utils.functional import cached_property
from django.db.models.constraints import UniqueConstraint
from django.db.models.indexes import Index

class CompositeCol(ExpressionList):
    def __init__(self, alias, target: "CompositeField", output_field=None):
        if output_field is None:
            output_field = target
        self.cols = [Col(alias, field) for field in target.component_fields]
        super().__init__(*self.cols, output_field=output_field)
        self.alias, self.target = alias, target

    def as_sql(self, compiler, connection):
        sql, params = super().as_sql(compiler, connection)
        return "(%s)" % sql, params

    def get_lookup(self, lookup):
        if lookup == "exact":
            return CompositeExact
        elif lookup == 'in':
            return CompositeIn
        return super().get_lookup(lookup)


class CompositeType:
    _composite_type = None
    _instance = None
    _repr_fmt = "()"
    _fields = ()

    @classmethod
    def namedtuple(cls, name, fields):
        fields = tuple(fields)
        doc_fields = ",".join(fields)
        repr_fields = ", ".join(f"{name}=%r" for name in fields)
        class_namespace = {
            "__doc__": f"{name}({doc_fields})",
            "_repr_fmt": f"({repr_fields})",
            "__slots__": ("_instance",),
            "_fields": fields,
        }

        def bind_property(name):
            def fget(self):
                return getattr(self._instance, name)

            def fset(self, value):
                return setattr(self._instance, name, value)

            return property(fget, fset, doc=f"Bind for field {name}")

        for field in fields:
            class_namespace[field] = bind_property(field)
        return type(name, (cls,), class_namespace)

    def resolve_expression(self, *args, **kwargs):
        def resolve_value(value):
            if isinstance(value, CompositeType):
                value = tuple(value)
            if hasattr(value, "resolve_expression"):
                value = value.resolve_expression(*args, **kwargs)
            elif isinstance(value, (list, tuple)):
                # The items of the iterable may be expressions and therefore need
                # to be resolved independently.
                values = (resolve_value(sub_value) for sub_value in value)
                type_ = type(value)
                if hasattr(type_, "_make"):  # namedtuple
                    return type_(*values)
                return type_(values)
            return value

        return resolve_value(self)

    def __init__(self, instance):
        self._instance = instance

    def __repr__(self):
        "Return a nicely formatted representation string"
        return self.__class__.__name__ + self._repr_fmt % tuple(self)

    def _asdict(self):
        "Return a new dict which maps field names to their values."
        return dict(zip(self._fields, self))

    def __hash__(self):
        return hash(self._fields)

    def __len__(self):
        return len(self._fields)

    def __iter__(self):
        return (getattr(self._instance, name) for name in self._fields)

    def __eq__(self, other):
        if isinstance(other, CompositeType):
            return tuple(self).__eq__(tuple(other))
        return tuple(self).__eq__(other)

    def __getnewargs__(self):
        "Return self as a plain tuple.  Used by copy and pickle."
        return tuple(self)

    def __getitem__(self, i):
        return getattr(self._instance, self._fields[i])

    def __setitem__(self, i, v):
        return setattr(self._instance, self._fields[i], v)


class CompositeDeferredAttribute(DeferredAttribute):
    def _bind(self, instance, cls):
        if issubclass(cls, CompositeType):
            return cls(instance)
        vals = [getattr(instance, name) for name in self.field.component_names]
        return cls(*vals)

    def __get__(self, instance, cls=None):
        if instance is None:
            return self
        data = instance.__dict__
        field_name = self.field.attname
        if field_name not in data:
            data[field_name] = self._bind(instance, cls=self.field.python_type)
        return data[field_name]

    def __set__(self, instance, value):
        if value is None:
            value = [None for _ in range(len(self.field.component_names))]
        for name, val in zip(self.field.component_names, value):
            setattr(instance, name, val)


class CompositeField(Field):
    component_names = None
    composite_columns = None
    python_type = tuple

    descriptor_class = CompositeDeferredAttribute

    def __init__(self, *names, **kwargs):
        kwargs["db_column"] = None
        kwargs["editable"] = False
        super().__init__(**kwargs)
        self.component_names = tuple(names)
        # this would deferred to post_init
        self.component_fields = None

    def get_attname_column(self):
        return self.get_attname(), self.db_column

    def contribute_to_class(self, cls, name, private_only=False) -> None:
        # logger.info(f"{cls.__name__} {name} {cls._meta} {self}")
        self.python_type = CompositeType.namedtuple(
            f"{cls.__name__}__{name}", self.component_names
        )
        super().contribute_to_class(cls, name, private_only)
        setattr(cls, self.attname, self.descriptor_class(self))

        if self.primary_key:
            cls._meta.pk = self
        if (self.unique or self.primary_key):
            cls._meta.constraints.append(
                UniqueConstraint(
                    fields=self.component_names, name=f"{cls.__name__}_{name}_unique"
                )
            )
        if self.db_index:
            cls._meta.indexes.append(Index(fields=self.component_names))

    def __hash__(self):
        return hash(self.component_fields)

    def get_col(self, alias, output_field=None):
        if alias == self.model._meta.db_table and (
            output_field is None or output_field == self
        ):
            return self.cached_col
        return CompositeCol(alias, self, output_field)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        args = list(self.component_names)

        # Handle the simpler arguments    
        return name, path, args, kwargs

    @cached_property
    def cached_col(self):
        return CompositeCol(self.model._meta.db_table, self)


def resolve_columns(*args, **kwargs):
    cls = kwargs.pop("sender")
    for field in cls._meta.local_fields:
        # TODO: is local_fields enough?
        if isinstance(field, CompositeField):
            field.component_fields = tuple(
                cls._meta.get_field(name) for name in field.component_names
            )

class_prepared.connect(resolve_columns)
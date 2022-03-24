import ast
import copy
import decimal
import os

from django.core import validators
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.utils.module_loading import import_string

from .errors import ValueRetrievalError, ValueProcessingError, ConfigurationError
from .utils import getargspec


def setup_value(target, name, value):
    actual_value = value.setup(name)
    # overwriting the original Value class with the result
    setattr(target, name, value.value)
    if value.multiple:
        for multiple_name, multiple_value in actual_value.items():
            setattr(target, multiple_name, multiple_value)


class Value:
    """
    A single settings value that is able to interpret env variables
    and implements a simple validation scheme.
    """
    multiple = False
    late_binding = False
    environ_required = False

    @property
    def value(self):
        value = self.default
        if not hasattr(self, '_value') and self.environ_name:
            self.setup(self.environ_name)
        if hasattr(self, '_value'):
            value = self._value
        return value

    @value.setter
    def value(self, value):
        self._value = value

    def __new__(cls, *args, **kwargs):
        """
        checks if the creation can end up directly in the final value.
        That is the case whenever environ = False or environ_name is given.
        """
        instance = object.__new__(cls)
        if 'late_binding' in kwargs:
            instance.late_binding = kwargs.get('late_binding')
        if not instance.late_binding:
            instance.__init__(*args, **kwargs)
            if ((instance.environ and instance.environ_name)
                    or (not instance.environ and instance.default)):
                instance = instance.setup(instance.environ_name)
        return instance

    def __init__(self, default=None, environ=True, environ_name=None,
                 environ_prefix='DJANGO', environ_required=False, help_text=None,
                 help_reference=None, *args, **kwargs):
        if isinstance(default, Value) and default.default is not None:
            self.default = copy.copy(default.default)
        else:
            self.default = default
        self.environ = environ
        if environ_prefix and environ_prefix.endswith('_'):
            environ_prefix = environ_prefix[:-1]
        self.environ_prefix = environ_prefix
        self.environ_name = environ_name
        self.environ_required = environ_required
        self.destination_name = None
        self.help_text = help_text
        self.help_reference = help_reference

    def __str__(self):
        return str(self.value)

    def __repr__(self):
        return repr(self.value)

    def __eq__(self, other):
        return self.value == other

    def __bool__(self):
        return bool(self.value)

    # Compatibility with python 2
    __nonzero__ = __bool__

    @property
    def full_environ_name(self):
        """
        The full name of the environment variable (including prefix and capitalization) from which this value should be
        retrieved
        """
        if self.environ_name:
            environ_name = self.environ_name
        else:
            environ_name = self.destination_name.upper()
        if self.environ_prefix:
            environ_name = '{0}_{1}'.format(self.environ_prefix, environ_name)
        return environ_name

    def setup(self, name):
        """
        Set up this value instance by retrieving the configured value from the environment and converting it to a native
        python data type

        :param name: Destination name for which this value is used.
            For example in the scenario of `DEBUG = Value()` in a `Configuration` subclass, this would be `DEBUG`.
        """
        self.destination_name = name
        value = self.default
        if self.environ:
            full_environ_name = self.full_environ_name
            if full_environ_name in os.environ:
                value = self.to_python(os.environ[full_environ_name])
            elif self.environ_required:
                raise ValueRetrievalError(self)
        self.value = value
        return value

    def to_python(self, value: str):
        """
        Convert the given value of an environment variable into an
        appropriate Python representation of the value.
        This should be overridden when subclassing.

        :param value: The value that should be converted to a python representation
        """
        return value


class MultipleMixin:
    multiple = True


class BooleanValue(Value):
    true_values = ('yes', 'y', 'true', '1')
    false_values = ('no', 'n', 'false', '0', '')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.environ_required and self.default not in (True, False):
            raise ImproperlyConfigured('Default value {0!r} is not a '
                                       'boolean value'.format(self.default))

    def to_python(self, value: str):
        normalized_value = value.strip().lower()
        if normalized_value in self.true_values:
            return True
        elif normalized_value in self.false_values:
            return False
        else:
            raise ValueProcessingError(self, value)


class CastingMixin:
    exception = (TypeError, ValueError)
    message = 'Cannot interpret value {0!r}'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self.caster, str):
            try:
                self._caster = import_string(self.caster)
            except ImportError as err:
                msg = "Could not import {!r}".format(self.caster)
                raise ImproperlyConfigured(msg) from err
        elif callable(self.caster):
            self._caster = self.caster
        else:
            error = 'Cannot use caster of {0} ({1!r})'.format(self,
                                                              self.caster)
            raise ImproperlyConfigured(error)
        try:
            arg_names = getargspec(self._caster)[0]
            self._params = {name: kwargs[name] for name in arg_names if name in kwargs}
        except TypeError:
            self._params = {}

    def to_python(self, value: str):
        try:
            if self._params:
                return self._caster(value, **self._params)
            else:
                return self._caster(value)
        except self.exception:
            raise ValueProcessingError(self, value)


class IntegerValue(CastingMixin, Value):
    caster = int


class PositiveIntegerValue(IntegerValue):

    def to_python(self, value: str):
        int_value = super().to_python(value)
        if int_value < 0:
            raise ValueProcessingError(self, value, f"Value needs to be positive or zero but {int_value} isn't")
        return int_value


class FloatValue(CastingMixin, Value):
    caster = float


class DecimalValue(CastingMixin, Value):
    caster = decimal.Decimal
    exception = decimal.InvalidOperation


class SequenceValue(Value):
    """
    Common code for sequence-type values (lists and tuples).
    Do not use this class directly. Instead use a subclass.
    """

    # Specify this value in subclasses, e.g. with 'list' or 'tuple'
    sequence_type = None
    converter = None

    def __init__(self, *args, **kwargs):
        msg = 'Cannot interpret {0} item in {0} {{0!r}}'
        self.message = msg.format(self.sequence_type.__name__)
        self.separator = kwargs.pop('separator', ',')
        converter = kwargs.pop('converter', None)
        if converter is not None:
            self.converter = converter
        super().__init__(*args, **kwargs)
        # make sure the default is the correct sequence type
        if self.default is None:
            self.default = self.sequence_type()
        else:
            self.default = self.sequence_type(self.default)
        # initial conversion
        if self.converter is not None:
            self.default = self._convert(self.default)

    def _convert(self, sequence):
        converted_values = []
        for value in sequence:
            try:
                converted_values.append(self.converter(value))
            except (TypeError, ValueError):
                raise ValueProcessingError(self, self.separator.join(sequence))
        return self.sequence_type(converted_values)

    def to_python(self, value: str):
        split_value = [v.strip() for v in value.strip().split(self.separator)]
        # removing empty items
        value_list = self.sequence_type(filter(None, split_value))
        if self.converter is not None:
            value_list = self._convert(value_list)
        return self.sequence_type(value_list)


class ListValue(SequenceValue):
    sequence_type = list


class TupleValue(SequenceValue):
    sequence_type = tuple


class SingleNestedSequenceValue(SequenceValue):
    """
    Common code for nested sequences (list of lists, or tuple of tuples).
    Do not use this class directly. Instead use a subclass.
    """

    def __init__(self, *args, **kwargs):
        self.seq_separator = kwargs.pop('seq_separator', ';')
        super().__init__(*args, **kwargs)

    def _convert(self, items):
        # This could receive either a bare or nested sequence
        if items and isinstance(items[0], self.sequence_type):
            converted_sequences = [
                super(SingleNestedSequenceValue, self)._convert(i) for i in items
            ]
            return self.sequence_type(converted_sequences)
        return self.sequence_type(super()._convert(items))

    def to_python(self, value: str):
        split_value = [
            v.strip() for v in value.strip().split(self.seq_separator)
        ]
        # Remove empty items
        filtered = self.sequence_type(filter(None, split_value))
        sequence = [
            super(SingleNestedSequenceValue, self).to_python(f) for f in filtered
        ]
        return self.sequence_type(sequence)


class SingleNestedListValue(SingleNestedSequenceValue):
    sequence_type = list


class SingleNestedTupleValue(SingleNestedSequenceValue):
    sequence_type = tuple


class BackendsValue(ListValue):

    def converter(self, value):
        try:
            import_string(value)
        except ImportError as err:
            raise ValueProcessingError(self, value)
        return value


class SetValue(ListValue):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.default is None:
            self.default = set()
        else:
            self.default = set(self.default)

    def to_python(self, value: str):
        return set(super().to_python(value))


class DictValue(Value):
    message = 'Cannot interpret dict value {0!r}'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.default is None:
            self.default = {}
        else:
            self.default = dict(self.default)

    def to_python(self, value: str):
        value = super().to_python(value)
        if not value:
            return {}
        try:
            evaled_value = ast.literal_eval(value)
        except ValueError:
            raise ValueProcessingError(self, value)
        if not isinstance(evaled_value, dict):
            raise ValueProcessingError(self, value)
        return evaled_value


class ValidationMixin:

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self.validator, str):
            try:
                self._validator = import_string(self.validator)
            except ImportError as err:
                msg = "Could not import {!r}".format(self.validator)
                raise ImproperlyConfigured(msg) from err
        elif callable(self.validator):
            self._validator = self.validator
        else:
            raise ImproperlyConfigured('Cannot use validator of '
                                       '{0} ({1!r})'.format(self, self.validator))
        if self.default:
            try:
                self.to_python(self.default)
            except ValueProcessingError as e:
                raise ImproperlyConfigured(e.main_error_msg) from e

    def to_python(self, value: str):
        try:
            self._validator(value)
        except ValidationError as e:
            raise ValueProcessingError(self, value, f"Validation failed: {e.message}")
        else:
            return value


class EmailValue(ValidationMixin, Value):
    message = 'Cannot interpret email value {0!r}'
    validator = 'django.core.validators.validate_email'


class URLValue(ValidationMixin, Value):
    message = 'Cannot interpret URL value {0!r}'
    validator = validators.URLValidator()


class IPValue(ValidationMixin, Value):
    message = 'Cannot interpret IP value {0!r}'
    validator = 'django.core.validators.validate_ipv46_address'


class RegexValue(ValidationMixin, Value):
    message = "Regex doesn't match value {0!r}"

    def __init__(self, *args, **kwargs):
        regex = kwargs.pop('regex', None)
        self.validator = validators.RegexValidator(regex=regex)
        super().__init__(*args, **kwargs)


class PathValue(Value):
    def __init__(self, *args, **kwargs):
        self.check_exists = kwargs.pop('check_exists', True)
        super().__init__(*args, **kwargs)

    def setup(self, name):
        value = super().setup(name)
        value = os.path.expanduser(value)
        if self.check_exists and not os.path.exists(value):
            raise ValueProcessingError(self, value, f"Path {value} does not exist")
        return os.path.abspath(value)


class SecretValue(Value):

    def __init__(self, *args, **kwargs):
        kwargs['environ'] = True
        kwargs['environ_required'] = True
        super().__init__(*args, **kwargs)
        if self.default is not None:
            raise ImproperlyConfigured('Secret values are only allowed to '
                                       'be set as environment variables')

    def setup(self, name):
        value = super().setup(name)
        if not value:
            raise ValueRetrievalError(self)
        return value


class EmailURLValue(CastingMixin, MultipleMixin, Value):
    caster = 'dj_email_url.parse'
    message = 'Cannot interpret email URL value {0!r}'
    late_binding = True

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('environ', True)
        kwargs.setdefault('environ_prefix', None)
        kwargs.setdefault('environ_name', 'EMAIL_URL')
        super().__init__(*args, **kwargs)
        if self.default is None:
            self.default = {}
        else:
            self.default = self.to_python(self.default)


class DictBackendMixin(Value):
    default_alias = 'default'

    def __init__(self, *args, **kwargs):
        self.alias = kwargs.pop('alias', self.default_alias)
        kwargs.setdefault('environ', True)
        kwargs.setdefault('environ_prefix', None)
        kwargs.setdefault('environ_name', self.environ_name)
        super().__init__(*args, **kwargs)
        if self.default is None:
            self.default = {}
        else:
            self.default = self.to_python(self.default)

    def to_python(self, value: str):
        value = super().to_python(value)
        return {self.alias: value}


class DatabaseURLValue(DictBackendMixin, CastingMixin, Value):
    caster = 'dj_database_url.parse'
    message = 'Cannot interpret database URL value {0!r}'
    environ_name = 'DATABASE_URL'
    late_binding = True


class CacheURLValue(DictBackendMixin, CastingMixin, Value):
    caster = 'django_cache_url.parse'
    message = 'Cannot interpret cache URL value {0!r}'
    environ_name = 'CACHE_URL'
    late_binding = True


class SearchURLValue(DictBackendMixin, CastingMixin, Value):
    caster = 'dj_search_url.parse'
    message = 'Cannot interpret Search URL value {0!r}'
    environ_name = 'SEARCH_URL'
    late_binding = True

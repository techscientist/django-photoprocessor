from django.db import models
from django.conf import settings
from django.utils import simplejson
from django.utils.encoding import force_unicode, smart_str
from django.core.files.storage import default_storage
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.serializers.json import DjangoJSONEncoder
from django import forms

from lib import Image
from utils import img_to_fobj
from processors import process_image

import logging
import os
import datetime

class JSONFieldDescriptor(object):
    def __init__(self, field):
        self.field = field

    def __get__(self, instance=None, owner=None):
        if instance is None:
            raise AttributeError(
                "The '%s' attribute can only be accessed from %s instances."
                % (self.field.name, owner.__name__))
        
        if not hasattr(instance, self.field.get_cache_name()):
            data = instance.__dict__.get(self.field.attname, dict())
            if not isinstance(data, dict):
                data = self.field.loads(data)
            setattr(instance, self.field.get_cache_name(), data)
        
        return getattr(instance, self.field.get_cache_name())

    def __set__(self, instance, value):
        instance.__dict__[self.field.attname] = value
        setattr(instance, self.field.get_cache_name(), value)


class JSONField(models.TextField):
    """
    A field for storing JSON-encoded data. The data is accessible as standard
    Python data types and is transparently encoded/decoded to/from a JSON
    string in the database.
    """
    serialize_to_string = True
    descriptor_class = JSONFieldDescriptor

    def __init__(self, verbose_name=None, name=None,
                 encoder=DjangoJSONEncoder(), decoder=simplejson.JSONDecoder(),
                 **kwargs):
        blank = kwargs.pop('blank', True)
        models.TextField.__init__(self, verbose_name, name, blank=blank,
                                  **kwargs)
        self.encoder = encoder
        self.decoder = decoder

    def db_type(self, connection=None):
        return "text"

    def contribute_to_class(self, cls, name):
        super(JSONField, self).contribute_to_class(cls, name)
        setattr(cls, self.name, self.descriptor_class(self))
    
    def pre_save(self, model_instance, add):
        "Returns field's value just before saving."
        descriptor = getattr(model_instance, self.attname)
        if descriptor.data is not None:
            return descriptor.data
        else:
            return self.field.value_from_object(model_instance)

    def get_db_prep_save(self, value, *args, **kwargs):
        if not isinstance(value, basestring):
            value = self.dumps(value)

        return super(JSONField, self).get_db_prep_save(value, *args, **kwargs)

    def value_to_string(self, obj):
        return self.dumps(self.value_from_object(obj))

    def dumps(self, data):
        return self.encoder.encode(data)

    def loads(self, val):
        try:
            val = self.decoder.decode(val)#, encoding=settings.DEFAULT_CHARSET)

            # XXX We need to investigate why this is happening once we have
            # a solid repro case.
            if isinstance(val, basestring):
                logging.warning("JSONField decode error. Expected dictionary, "
                                "got string for input '%s'" % val)
                # For whatever reason, we may have gotten back
                val = self.decoder.decode(val)#, encoding=settings.DEFAULT_CHARSET)
        except ValueError:
            val = None
        return val
    
    def south_field_triple(self):
        "Returns a suitable description of this field for South."
        # We'll just introspect the _actual_ field.
        from south.modelsinspector import introspector
        field_class = "django.db.models.fields.TextField"
        args, kwargs = introspector(self)
        # That's our definition!
        return (field_class, args, kwargs)

from django.db.models.fields.files import FieldFile, FileDescriptor

class ImageWithProcessorsFieldFile(FieldFile):
    def __init__(self, instance, field, data):
        self.data = data
        name = self.data.get('original', None)
        FieldFile.__init__(self, instance, field, name)
    
    def __getitem__(self, key):
        if key in self.field.thumbnails:
            if key in self.data:
                return FieldFile(self.instance, self.field, self.data[key]['path'])
            return FieldFile(self.instance, self.field, None)
        raise KeyError
    
    def save(self, name, content, save=True):
        name = self.field.generate_filename(self.instance, name)
        self.name = self.storage.save(name, content)
        self.data['original'] = self.name

        # Update the filesize cache
        self._size = content.size
        self._committed = True
        
        #now update the children
        base_name, base_ext = name.split('/')[-1].split('.', 1)
        source_image = Image.open(self.file)
        for key, config in self.field.thumbnails.iteritems(): #TODO rename to specs
            if key in self.data and self.data[key].get('config') == config:
                continue
            img, fmt = process_image(source_image, config)
            
            thumb_name = '%s-%s.%s' % (base_name, key, base_ext)
            
            thumb_name = self.field.generate_filename(self.instance, thumb_name)
            #not efficient, requires image to be loaded into memory
            thumb_fobj = ContentFile(img_to_fobj(img, fmt).read())
            thumb_name = self.storage.save(thumb_name, thumb_fobj)
            
            self.data[key] = {'path':thumb_name, 'config':config}

        # Save the object because it has changed, unless save is False
        if save:
            self.instance.save()
    save.alters_data = True

    def delete(self, save=True):
        # Only close the file if it's already open, which we know by the
        # presence of self._file
        if hasattr(self, '_file'):
            self.close()
            del self.file

        self.storage.delete(self.name)

        self.name = None
        self.data['original'] = self.name

        # Delete the filesize cache
        if hasattr(self, '_size'):
            del self._size
        self._committed = False

        if save:
            self.instance.save()
    delete.alters_data = True

class ImageWithProcessorsDesciptor(JSONFieldDescriptor):
    def __get__(self, instance=None, owner=None):
        if instance is None:
            raise AttributeError(
                "The '%s' attribute can only be accessed from %s instances."
                % (self.field.name, owner.__name__))
        
        data = JSONFieldDescriptor.__get__(self, instance, owner)
        
        return self.field.attr_class(instance, self.field, data)

    def __set__(self, instance, value):
        pass
        #data = getattr(instance, self.field.attname)
        #data['original'] = value

class ImageWithProcessorsField(JSONField):
    descriptor_class = ImageWithProcessorsDesciptor
    attr_class = ImageWithProcessorsFieldFile
    
    def __init__(self, **kwargs):
        self.thumbnails = kwargs.pop('thumbnails')
        self.upload_to = kwargs.pop('upload_to')
        self.storage = kwargs.pop('storage', default_storage)
        JSONField.__init__(self, **kwargs)
    
    def contribute_to_class(self, cls, name):
        super(ImageWithProcessorsField, self).contribute_to_class(cls, name)
        setattr(cls, self.name, self.descriptor_class(self))
    
    def get_directory_name(self):
        return os.path.normpath(force_unicode(datetime.datetime.now().strftime(smart_str(self.upload_to))))

    def get_filename(self, filename):
        return os.path.normpath(self.storage.get_valid_name(os.path.basename(filename)))

    def generate_filename(self, instance, filename):
        return os.path.join(self.get_directory_name(), self.get_filename(filename))
    
    def save_form_data(self, instance, data):
        # Important: None means "no change", other false value means "clear"
        # This subtle distinction (rather than a more explicit marker) is
        # needed because we need to consume values that are also sane for a
        # regular (non Model-) Form to find in its cleaned_data dictionary.
        if data is not None:
            # This value will be converted to unicode and stored in the
            # database, so leaving False as-is is not acceptable.
            if not data:
                data = ''
            setattr(instance, self.name, data)

    def formfield(self, **kwargs):
        defaults = {'form_class': forms.FileField, 'max_length': self.max_length}
        # If a file has been provided previously, then the form doesn't require
        # that a new file is provided this time.
        # The code to mark the form field as not required is used by
        # form_for_instance, but can probably be removed once form_for_instance
        # is gone. ModelForm uses a different method to check for an existing file.
        if 'initial' in kwargs:
            defaults['required'] = False
        defaults.update(kwargs)
        return super(ImageWithProcessorsField, self).formfield(**defaults)

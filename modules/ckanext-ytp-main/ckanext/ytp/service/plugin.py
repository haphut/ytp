from ckan import plugins
from ckan.plugins import toolkit
from ckan.common import c, request

from ckanext.ytp.common.converters import to_list_json, is_url
from ckanext.ytp.common.tools import add_translation_modify_schema, add_languages_modify, add_languages_show

import logging

log = logging.getLogger(__name__)


class YTPServiceForm(plugins.SingletonPlugin, toolkit.DefaultDatasetForm):
    plugins.implements(plugins.IDatasetForm, inherit=True)
    plugins.implements(plugins.IConfigurer, inherit=True)

    _localized_fields = []

    def update_config(self, config):
        toolkit.add_public_directory(config, '/var/www/resources')
        toolkit.add_resource('public/javascript/', 'ytp_service_js')
        toolkit.add_template_directory(config, 'templates')

        toolkit.add_resource('../common/public/javascript/', 'ytp_common_js')
        toolkit.add_template_directory(config, '../common/templates')

    # IDatasetForm #

    def _modify_package_schema(self, schema):
        ignore_missing = toolkit.get_validator('ignore_missing')
        convert_to_extras = toolkit.get_converter('convert_to_extras')

        schema.update({'collection_type': [ignore_missing, unicode, convert_to_extras]})
        schema.update({'extra_information': [ignore_missing, is_url, to_list_json, convert_to_extras]})

        schema = add_translation_modify_schema(schema)
        schema = add_languages_modify(schema, self._localized_fields)

        return schema

    def create_package_schema(self):
        schema = super(YTPServiceForm, self).create_package_schema()
        return self._modify_package_schema(schema)

    def update_package_schema(self):
        schema = super(YTPServiceForm, self).update_package_schema()
        return self._modify_package_schema(schema)

    def show_package_schema(self):
        schema = super(YTPServiceForm, self).show_package_schema()

        ignore_missing = toolkit.get_validator('ignore_missing')
        convert_from_extras = toolkit.get_converter('convert_from_extras')

        schema['tags']['__extras'].append(toolkit.get_converter('free_tags_only'))
        schema.update({'collection_type': [convert_from_extras, ignore_missing]})
        schema = add_languages_show(schema, self._localized_fields)
        return schema

    def package_types(self):
        return ['service']

    def is_fallback(self):
        return False

    def new_template(self):
        return 'package/ytp/service/new.html'

    def package_form(self):
        return 'package/ytp/service/new_package_form.html'

    def setup_template_variables(self, context, data_dict):
        c.preselected_group = request.params.get('group', None)
        super(YTPServiceForm, self).setup_template_variables(context, data_dict)
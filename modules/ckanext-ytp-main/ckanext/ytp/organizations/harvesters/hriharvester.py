import urllib2

from ckan.lib.base import c
from ckan import model
from ckan.model import Session
from ckan.logic import ValidationError, NotFound, get_action
from ckan.lib.helpers import json
from ckan.lib.munge import munge_name

from ckanext.harvest.model import (HarvestJob, HarvestObject, HarvestObjectExtra)

import logging
log = logging.getLogger(__name__)

from ckanext.harvest.harvesters.base import HarvesterBase

DELETE = "delete"


class HRIHarvester(HarvesterBase):
    '''
    A Harvester for HRI.fi
    '''
    config = None

    api_version = 2
    action_api_version = 3

    def _get_rest_api_offset(self):
        return '/api/%d/rest' % self.api_version

    def _get_action_api_offset(self):
        return '/api/%d/action' % self.action_api_version

    def _get_search_api_offset(self):
        return '/api/%d/search' % self.api_version

    def _get_content(self, url):
        http_request = urllib2.Request(
            url=url,
        )

        api_key = self.config.get('api_key', None)
        if api_key:
            http_request.add_header('Authorization', api_key)

        try:
            http_response = urllib2.urlopen(http_request)
        except urllib2.URLError, e:
            raise ContentFetchError(
                'Could not fetch url: %s, error: %s' %
                (url, str(e))
            )
        return http_response.read()

    def _get_group(self, base_url, group_name):
        url = base_url + self._get_rest_api_offset() + '/group/' + munge_name(group_name)
        try:
            content = self._get_content(url)
            return json.loads(content)
        except (ContentFetchError, ValueError):
            log.debug('Could not fetch/decode remote group')
            raise RemoteResourceError('Could not fetch/decode remote group')

    def _get_organization(self, base_url, org_name):
        url = base_url + self._get_action_api_offset() + '/organization_show?id=' + org_name
        try:
            content = self._get_content(url)
            content_dict = json.loads(content)
            return content_dict['result']
        except (ContentFetchError, ValueError, KeyError):
            log.debug('Could not fetch/decode remote group')
            raise RemoteResourceError('Could not fetch/decode remote organization')

    def _set_config(self, config_str):
        if config_str:
            self.config = json.loads(config_str)
            if 'api_version' in self.config:
                self.api_version = int(self.config['api_version'])

            log.debug('Using config: %r', self.config)
        else:
            self.config = {}

    def info(self):
        return {
            'name': 'hri',
            'title': 'HRI',
            'description': 'Harvests hri.fi',
            'form_config_interface': 'Text'
        }

    def validate_config(self, config):
        if not config:
            return config

        try:
            config_obj = json.loads(config)

            if 'api_version' in config_obj:
                try:
                    int(config_obj['api_version'])
                except ValueError:
                    raise ValueError('api_version must be an integer')

            if 'default_tags' in config_obj:
                if not isinstance(config_obj['default_tags'], list):
                    raise ValueError('default_tags must be a list')

            if 'default_groups' in config_obj:
                if not isinstance(config_obj['default_groups'], list):
                    raise ValueError('default_groups must be a list')

                # Check if default groups exist
                context = {'model': model, 'user': c.user}
                for group_name in config_obj['default_groups']:
                    try:
                        get_action('group_show')(context, {'id': group_name})
                    except NotFound, e:
                        raise ValueError('Default group not found')

            if 'default_extras' in config_obj:
                if not isinstance(config_obj['default_extras'], dict):
                    raise ValueError('default_extras must be a dictionary')

            if 'user' in config_obj:
                # Check if user exists
                context = {'model': model, 'user': c.user}
                try:
                    get_action('user_show')(context, {'id': config_obj.get('user')})
                except NotFound, e:
                    raise ValueError('User not found')

            for key in ('read_only', 'force_all', 'manage_deletions'):
                if key in config_obj:
                    if not isinstance(config_obj[key], bool):
                        raise ValueError('%s must be boolean' % key)

        except ValueError, e:
            raise e

        return config

    def _get_deleted_packages(self, url, harvest_job):
        """
        Checks all packages on remote instance against ones existing, returns
        the difference
        """
        # Prior to 2.0, https://github.com/okfn/ckan/pull/545 the api returns
        # a 403 error. Deletions will appear in the list of revisions in the
        # gather stage but will error during the import stage. In order to
        # harvest < 2.0 ckan instances I've written this mess.
        try:
            content = self._get_content(url)
            remote_packages = json.loads(content)
        except urllib2.URLError, e:
            self._save_gather_error('Unable to get all packages for URL: %s: %s' % (url, str(e)), harvest_job)
        except ValueError, e:
            self._save_gather_error('Unable to decode JSON for URL: %s: %s' % (url, str(e)), harvest_job)

        existing_packages = model.Session.query(HarvestObject.guid) \
            .filter(HarvestObject.current == True).filter(HarvestObject.harvest_source_id == harvest_job.source_id)  # noqa
        if len(existing_packages.all()) == 0:
            return []

        deleted_ids = set(zip(*existing_packages.all())[0]) - set(remote_packages)

        deleted = []
        for package_id in deleted_ids:
            harvest_object = HarvestObject(job=harvest_job,
                                           extras=[HarvestObjectExtra(key='status', value=DELETE)],
                                           package_id=package_id)

            harvest_object.save()
            deleted.append(harvest_object.id)
        return deleted

    def gather_stage(self, harvest_job):
        log.debug('In CKANHarvester gather_stage (%s)' % harvest_job.source.url)
        get_all_packages = True
        package_ids = []

        self._set_config(harvest_job.source.config)

        # Check if this source has been harvested before
        previous_job = Session.query(HarvestJob) \
            .filter(HarvestJob.source == harvest_job.source) \
            .filter(HarvestJob.id != harvest_job.id) \
            .filter(HarvestJob.gather_finished != None).order_by(HarvestJob.gather_finished.desc()).limit(1).first()  # noqa

        # Get source URL
        base_url = harvest_job.source.url.rstrip('/')
        base_rest_url = base_url + self._get_rest_api_offset()
        base_search_url = base_url + self._get_search_api_offset()

        if (previous_job and not previous_job.gather_errors and not len(previous_job.objects) == 0):
            if not self.config.get('force_all', False):
                get_all_packages = False

                # Request only the packages modified since last harvest job
                last_time = previous_job.gather_finished.isoformat()
                url = base_search_url + '/revision?since_time=%s' % last_time

                try:
                    content = self._get_content(url)

                    revision_ids = json.loads(content)
                    if len(revision_ids):
                        for revision_id in revision_ids:
                            url = base_rest_url + '/revision/%s' % revision_id
                            try:
                                content = self._get_content(url)
                            except ContentFetchError, e:
                                self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)), harvest_job)
                                continue

                            revision = json.loads(content)
                            for package_id in revision['packages']:
                                if package_id not in package_ids:
                                    package_ids.append(package_id)
                    else:
                        log.info('No packages have been updated on the remote CKAN instance since the last harvest job')
                        return []

                except urllib2.HTTPError, e:
                    if e.getcode() == 400:
                        log.info('CKAN instance %s does not suport revision filtering' % base_url)
                        get_all_packages = True
                    else:
                        self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)), harvest_job)
                        return None

        if get_all_packages:
            # Request all remote packages
            url = base_rest_url + '/package'
            try:
                content = self._get_content(url)
            except ContentFetchError, e:
                self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)), harvest_job)
                return None

            package_ids = json.loads(content)

        try:
            object_ids = []
            if self.config.get('manage_deletions', False):
                missing_ids = self._get_deleted_packages(base_rest_url + '/package', harvest_job)
                if missing_ids:
                    object_ids.extend(missing_ids)

            if len(package_ids):
                for package_id in package_ids:
                    # Create a new HarvestObject for this identifier
                    obj = HarvestObject(guid=package_id, job=harvest_job)
                    obj.save()
                    object_ids.append(obj.id)

            if package_ids or missing_ids:
                return object_ids

            else:
                self._save_gather_error('No packages received for URL: %s' % url, harvest_job)
                return None
        except Exception, e:
            self._save_gather_error('%r' % e.message, harvest_job)

    def fetch_stage(self, harvest_object):
        log.debug('In CKANHarvester fetch_stage')

        self._set_config(harvest_object.job.source.config)

        for extra in harvest_object.extras:
            if extra.key == 'status' and extra.value == DELETE:
                harvest_object.content = DELETE
                harvest_object.save()
                return True

        # Get source URL
        url = harvest_object.source.url.rstrip('/')
        url = url + self._get_rest_api_offset() + '/package/' + harvest_object.guid

        # Get contents
        try:
            content = self._get_content(url)
        except ContentFetchError, e:
            self._save_object_error('Unable to get content for package: %s: %r' %
                                    (url, e), harvest_object)
            return None

        # Save the fetched contents in the HarvestObject
        harvest_object.content = content
        harvest_object.save()
        return True

    def import_stage(self, harvest_object):
        log.debug('In CKANHarvester import_stage')
        if not harvest_object:
            log.error('No harvest object received')
            return False

        if harvest_object.content is None:
            self._save_object_error('Empty content for object %s' % harvest_object.id,
                                    harvest_object, 'Import')
            return False

        self._set_config(harvest_object.job.source.config)

        if harvest_object.content == DELETE:
            return self._delete_package(harvest_object)

        try:
            package_dict = json.loads(harvest_object.content)

            if package_dict.get('type') == 'harvest':
                log.warn('Remote dataset is a harvest source, ignoring...')
                return True

            # Set default tags if needed
            default_tags = self.config.get('default_tags', [])
            if default_tags:
                if 'tags' not in package_dict:
                    package_dict['tags'] = []
                package_dict['tags'].extend([t for t in default_tags if t not in package_dict['tags']])

            remote_groups = self.config.get('remote_groups', None)
            if remote_groups not in ('only_local', 'create'):
                # Ignore remote groups
                package_dict.pop('groups', None)
            else:
                if 'groups' not in package_dict:
                    package_dict['groups'] = []

                # check if remote groups exist locally, otherwise remove
                validated_groups = []
                context = {'model': model, 'session': Session, 'user': 'harvest'}

                for group_name in package_dict['groups']:
                    try:
                        data_dict = {'id': group_name}
                        group = get_action('group_show')(context, data_dict)
                        if self.api_version == 1:
                            validated_groups.append(group['name'])
                        else:
                            validated_groups.append(group['id'])
                    except NotFound, e:
                        log.info('Group %s is not available' % group_name)
                        if remote_groups == 'create':
                            try:
                                group = self._get_group(harvest_object.source.url, group_name)
                            except RemoteResourceError:
                                log.error('Could not get remote group %s' % group_name)
                                continue

                            for key in ['packages', 'created', 'users', 'groups', 'tags', 'extras', 'display_name']:
                                group.pop(key, None)
                            get_action('group_create')(context, group)
                            log.info('Group %s has been newly created' % group_name)
                            if self.api_version == 1:
                                validated_groups.append(group['name'])
                            else:
                                validated_groups.append(group['id'])

                package_dict['groups'] = validated_groups

            context = {'model': model, 'session': Session, 'user': 'harvest'}

            # Local harvest source organization
            source_dataset = get_action('package_show')(context, {'id': harvest_object.source.id})
            source_dataset.get('owner_org')

            self.config.get('remote_orgs', None)

            if 'owner_org' not in package_dict:
                package_dict['owner_org'] = None

            # check if remote org exist locally, otherwise remove
            validated_org = None
            remote_org = None
            if package_dict.get('organization'):
                remote_org = package_dict['organization']['name']

            if remote_org:
                try:
                    data_dict = {'id': remote_org}
                    org = get_action('organization_show')(context, data_dict)
                    validated_org = org['id']
                except NotFound:
                    log.info('No organization exist, not importing dataset')
                    return "unchanged"
            else:
                log.info('No organization in harvested dataset')
                return "unchanged"

            package_dict['owner_org'] = validated_org

            # Set default groups if needed
            default_groups = self.config.get('default_groups', [])
            if default_groups:
                if 'groups' not in package_dict:
                    package_dict['groups'] = []
                package_dict['groups'].extend([g for g in default_groups if g not in package_dict['groups']])

            # Find any extras whose values are not strings and try to convert
            # them to strings, as non-string extras are not allowed anymore in
            # CKAN 2.0.
            for key in package_dict['extras'].keys():
                if not isinstance(package_dict['extras'][key], basestring):
                    try:
                        package_dict['extras'][key] = json.dumps(
                            package_dict['extras'][key])
                    except TypeError:
                        # If converting to a string fails, just delete it.
                        del package_dict['extras'][key]

            # Set default extras if needed
            default_extras = self.config.get('default_extras', {})
            if default_extras:
                override_extras = self.config.get('override_extras', False)
                if 'extras' not in package_dict:
                    package_dict['extras'] = {}
                for key, value in default_extras.iteritems():
                    if key not in package_dict['extras'] or override_extras:
                        # Look for replacement strings
                        if isinstance(value, basestring):
                            value = value.format(harvest_source_id=harvest_object.job.source.id,
                                                 harvest_source_url=harvest_object.job.source.url.strip('/'),
                                                 harvest_source_title=harvest_object.job.source.title,
                                                 harvest_job_id=harvest_object.job.id,
                                                 harvest_object_id=harvest_object.id,
                                                 dataset_id=package_dict['id'])

                        package_dict['extras'][key] = value

            # Clear remote url_type for resources (eg datastore, upload) as we
            # are only creating normal resources with links to the remote ones
            for resource in package_dict.get('resources', []):
                resource.pop('url_type', None)

            # Check if package exists
            data_dict = {}
            data_dict['id'] = package_dict['id']
            try:
                existing_package_dict = get_action('package_show')(context, data_dict)
                if 'metadata_modified' in package_dict and package_dict['metadata_modified'] <= existing_package_dict.get('metadata_modified'):
                    return "unchanged"
            except NotFound:
                pass

            result = self._create_or_update_package(package_dict, harvest_object)

            if result and self.config.get('read_only', False) is True:

                package = model.Package.get(package_dict['id'])

                # Clear default permissions
                model.clear_user_roles(package)

                # Setup harvest user as admin
                user_name = self.config.get('user', u'harvest')
                user = model.User.get(user_name)
                model.PackageRole(package=package, user=user, role=model.Role.ADMIN)

                # Other users can only read
                for user_name in (u'visitor', u'logged_in'):
                    user = model.User.get(user_name)
                    model.PackageRole(package=package, user=user, role=model.Role.READER)

            return True
        except ValidationError, e:
            self._save_object_error('Invalid package with GUID %s: %r' % (harvest_object.guid, e.error_dict),
                                    harvest_object, 'Import')
        except Exception, e:
            self._save_object_error('%r' % e, harvest_object, 'Import')


class ContentFetchError(Exception):
    pass


class RemoteResourceError(Exception):
    pass

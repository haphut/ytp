from ckan import model
from ckan.common import c, _
from ckan.logic import get_action, NotFound, NotAuthorized
from ckan.controllers.organization import OrganizationController
from ckan.lib.base import abort
from ckan.logic import check_access

import logging

log = logging.getLogger(__name__)


class YtpOrganizationController(OrganizationController):
    def members(self, id):
        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author}

        try:
            c.members = self._action('member_list')(
                context, {'id': id, 'object_type': 'user'}
            )
            c.group_dict = self._action('group_show')(context, {'id': id})

            check_access('group_update', context, {'id': id})
            context['keep_email'] = True
            context['auth_user_obj'] = c.userobj
            context['return_minimal'] = True

            members = []
            for user_id, name, role in c.members:
                user_dict = {'id': user_id}
                data = get_action('user_show')(context, user_dict)
                if data['state'] != 'deleted':
                    members.append((user_id, data['name'], role, data['email']))

            c.members = members
        except NotAuthorized:
            abort(401, _('Unauthorized to view group %s members') % id)
        except NotFound:
            abort(404, _('Group not found'))
        return self._render_template('group/members.html')

    def user_list(self):
        if c.userobj and c.userobj.sysadmin:

            q = model.Session.query(model.Group, model.Member, model.User). \
                filter(model.Member.group_id == model.Group.id). \
                filter(model.Member.table_id == model.User.id). \
                filter(model.Member.table_name == 'user'). \
                filter(model.User.name != 'harvest'). \
                filter(model.User.name != 'default'). \
                filter(model.User.state == 'active')

            users = []

            for group, member, user in q.all():
                users.append({
                    'user_id': user.id,
                    'username': user.name,
                    'organization': group.title,
                    'role': member.capacity,
                    'email': user.email
                })

            c.users = users
        else:
            c.users = []

        return self._render_template('group/user_list.html')

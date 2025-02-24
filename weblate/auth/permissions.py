# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from django.conf import settings

from weblate.trans.models import (
    Component,
    ComponentList,
    ContributorAgreement,
    Project,
    Translation,
    Unit,
)
from weblate.utils.stats import ProjectLanguage

SPECIALS = {}


def register_perm(*perms):
    def wrap_perm(function):
        for perm in perms:
            SPECIALS[perm] = function
        return function

    return wrap_perm


def check_global_permission(user, permission, obj):
    """Generic permission check for base classes."""
    if user.is_superuser:
        return True
    return user.groups.filter(roles__permissions__codename=permission).exists()


def check_permission(user, permission, obj):
    """Generic permission check for base classes."""
    if user.is_superuser:
        return True
    if isinstance(obj, ProjectLanguage):
        obj = obj.project
    if isinstance(obj, Project):
        return any(
            permission in permissions
            for permissions, _langs in user.get_project_permissions(obj)
        )
    if isinstance(obj, ComponentList):
        return all(
            check_permission(user, permission, component)
            for component in obj.components.iterator()
        )
    if isinstance(obj, Component):
        return (
            not obj.restricted
            and any(
                permission in permissions
                for permissions, _langs in user.get_project_permissions(obj.project)
            )
        ) or any(
            permission in permissions
            for permissions, _langs in user.component_permissions[obj.pk]
        )
    if isinstance(obj, Translation):
        lang = obj.language_id
        return (
            not obj.component.restricted
            and any(
                permission in permissions and (langs is None or lang in langs)
                for permissions, langs in user.get_project_permissions(
                    obj.component.project
                )
            )
        ) or any(
            permission in permissions and (langs is None or lang in langs)
            for permissions, langs in user.component_permissions[obj.component_id]
        )
    raise ValueError(f"Permission {permission} does not support: {obj.__class__}")


@register_perm("comment.resolve", "comment.delete", "suggestion.delete")
def check_delete_own(user, permission, obj):
    if user.is_authenticated and obj.user == user:
        return True
    return check_permission(user, permission, obj.unit.translation)


@register_perm("unit.check")
def check_ignore_check(user, permission, check):
    if check.is_enforced():
        return False
    return check_permission(user, permission, check.unit.translation)


def check_can_edit(user, permission, obj, is_vote=False):
    translation = component = None

    if isinstance(obj, Translation):
        translation = obj
        component = obj.component
        project = component.project
    elif isinstance(obj, Component):
        component = obj
        project = component.project
    elif isinstance(obj, Project):
        project = obj
    elif isinstance(obj, ProjectLanguage):
        project = obj.project
    else:
        raise TypeError(f"Unknown object for permission check: {obj.__class__}")

    # Email is needed for user to be able to edit
    if user.is_authenticated and not user.email:
        return False

    if component:
        # Check component lock
        if component.locked:
            return False

        # Check contributor agreement
        if component.agreement and not ContributorAgreement.objects.has_agreed(
            user, component
        ):
            return False

    # Perform usual permission check
    if not check_permission(user, permission, obj):
        return False

    # Special check for source strings (templates)
    if (
        translation
        and translation.is_template
        and not check_permission(user, "unit.template", obj)
    ):
        return False

    # Special checks for voting
    if is_vote and component and not component.suggestion_voting:
        return False
    if (
        not is_vote
        and translation
        and component.suggestion_voting
        and component.suggestion_autoaccept > 0
        and not check_permission(user, "unit.override", obj)
    ):
        return False

    # Billing limits
    if not project.paid:
        return False

    return True


@register_perm("unit.review")
def check_unit_review(user, permission, obj, skip_enabled=False):
    if not skip_enabled:
        if isinstance(obj, Translation):
            if not obj.enable_review:
                return False
        else:
            if isinstance(obj, (Component, ProjectLanguage)):
                project = obj.project
            else:
                project = obj
            if not project.source_review and not project.translation_review:
                return False
    return check_can_edit(user, permission, obj)


@register_perm("unit.edit", "suggestion.accept")
def check_edit_approved(user, permission, obj):
    component = None
    if isinstance(obj, Unit):
        unit = obj
        obj = unit.translation
        # Read only check is unconditional as there is another one
        # in PluralTextarea.render
        if unit.readonly or (
            unit.approved
            and not check_unit_review(user, "unit.review", obj, skip_enabled=True)
        ):
            return False
    if isinstance(obj, Translation):
        component = obj.component
        if obj.is_readonly:
            return False
    elif isinstance(obj, Component):
        component = obj
    if component is not None and component.is_glossary:
        permission = "glossary.edit"
    return check_can_edit(user, permission, obj)


def check_manage_units(translation: Translation, component: Component) -> bool:
    if not isinstance(component, Component):
        return False
    source = translation.is_source
    template = component.has_template()
    # Add only to source in monolingual
    if not source and template:
        return False
    # Check if adding is generally allowed
    if not component.manage_units or (template and not component.edit_template):
        return False
    return True


@register_perm("unit.delete")
def check_unit_delete(user, permission, obj):
    if isinstance(obj, Unit):
        obj = obj.translation
    component = obj.component
    # Check if removing is generally allowed
    if not check_manage_units(obj, component):
        return False

    # Does file format support removing?
    if not component.file_format_cls.can_delete_unit:
        return False

    if component.is_glossary:
        permission = "glossary.delete"
    return check_can_edit(user, permission, obj)


@register_perm("unit.add")
def check_unit_add(user, permission, translation):
    component = translation.component
    # Check if adding is generally allowed
    if not check_manage_units(translation, component):
        return False

    # Does file format support adding?
    if not component.file_format_cls.can_add_unit:
        return False

    if component.is_glossary:
        permission = "glossary.add"

    return check_can_edit(user, permission, translation)


@register_perm("translation.add")
def check_translation_add(user, permission, component):
    if component.new_lang == "none" and not component.can_add_new_language(
        user, fast=True
    ):
        return False
    if component.locked:
        return False
    return check_permission(user, permission, component)


@register_perm("translation.auto")
def check_autotranslate(user, permission, translation):
    if isinstance(translation, Translation) and (
        (translation.is_source and not translation.component.intermediate)
        or translation.is_readonly
    ):
        return False
    return check_can_edit(user, permission, translation)


@register_perm("suggestion.vote")
def check_suggestion_vote(user, permission, obj):
    if isinstance(obj, Unit):
        obj = obj.translation
    return check_can_edit(user, permission, obj, is_vote=True)


@register_perm("suggestion.add")
def check_suggestion_add(user, permission, obj):
    if isinstance(obj, Unit):
        obj = obj.translation
    if not obj.component.enable_suggestions or obj.is_readonly:
        return False
    # Check contributor agreement
    if obj.component.agreement and not ContributorAgreement.objects.has_agreed(
        user, obj.component
    ):
        return False
    return check_permission(user, permission, obj)


@register_perm("upload.perform")
def check_contribute(user, permission, translation):
    # Bilingual source translations
    if translation.is_source and not translation.is_template:
        return hasattr(
            translation.component.file_format_cls, "update_bilingual"
        ) and user.has_perm("source.edit", translation)
    if translation.component.is_glossary:
        permission = "glossary.upload"
    return check_can_edit(user, permission, translation) and (
        # Normal upload
        check_edit_approved(user, "unit.edit", translation)
        # Suggestion upload
        or check_suggestion_add(user, "suggestion.add", translation)
        # Add upload
        or check_suggestion_add(user, "unit.add", translation)
        # Source upload
        or (translation.is_source and user.has_perm("source.edit", translation))
    )


@register_perm("machinery.view")
def check_machinery(user, permission, obj):
    # No machinery for source without intermediate language
    if (
        isinstance(obj, Translation)
        and obj.is_source
        and not obj.component.intermediate
    ):
        return False

    # Check the actual machinery.view permission
    if not check_permission(user, permission, obj):
        return False

    # Only show machinery to users allowed to translate or suggest
    return check_edit_approved(user, "unit.edit", obj) or check_suggestion_add(
        user, "suggestion.add", obj
    )


@register_perm("translation.delete")
def check_translation_delete(user, permission, obj):
    if obj.is_source:
        return False
    return check_permission(user, permission, obj)


@register_perm("reports.view", "change.download")
def check_possibly_global(user, permission, obj):
    if obj is None:
        return user.is_superuser
    return check_permission(user, permission, obj)


@register_perm("meta:vcs.status")
def check_repository_status(user, permission, obj):
    return (
        check_permission(user, "vcs.push", obj)
        or check_permission(user, "vcs.commit", obj)
        or check_permission(user, "vcs.reset", obj)
        or check_permission(user, "vcs.update", obj)
    )


@register_perm("meta:team.edit")
def check_team_edit(user, permission, obj):
    return check_global_permission(user, "group.edit", obj) or (
        obj.defining_project
        and check_permission(user, "project.permissions", obj.defining_project)
    )


@register_perm("meta:team.users")
def check_team_edit_users(user, permission, obj):
    return (
        check_team_edit(user, permission, obj) or obj.pk in user.administered_group_ids
    )


@register_perm("billing.view")
def check_billing_view(user, permission, obj):
    if hasattr(obj, "all_projects"):
        if user.is_superuser or obj.owners.filter(pk=user.pk).exists():
            return True
        # This is a billing object
        return any(check_permission(user, permission, prj) for prj in obj.all_projects)
    return check_permission(user, permission, obj)


@register_perm("billing:project.permissions")
def check_billing(user, permission, obj):
    if "weblate.billing" in settings.INSTALLED_APPS and not any(
        billing.plan.change_access_control for billing in obj.billings
    ):
        return False

    return check_permission(user, "project.permissions", obj)


# This does not exist for real
@register_perm("announcement.delete")
def check_announcement_delete(user, permission, obj):
    return (
        user.is_superuser
        or (obj.component and check_permission(user, "component.edit", obj.component))
        or (obj.project and check_permission(user, "project.edit", obj.project))
    )


# This does not exist for real
@register_perm("unit.flag")
def check_unit_flag(user, permission, obj):
    if isinstance(obj, Unit):
        obj = obj.translation
    if not obj.component.is_glossary or obj.is_source:
        return user.has_perm("source.edit", obj)

    return user.has_perm("glossary.edit", obj)


@register_perm("memory.edit", "memory.delete")
def check_memory_perms(user, permission, memory):
    from weblate.memory.models import Memory

    if isinstance(memory, Memory):
        if memory.user_id == user.id:
            return True
        if memory.project is None:
            return user.is_superuser
        project = memory.project
    else:
        project = memory
    return check_permission(user, permission, project)

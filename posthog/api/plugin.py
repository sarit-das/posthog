import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Set, cast, Literal

import requests
from dateutil.relativedelta import relativedelta
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.uploadedfile import UploadedFile
from django.db import connections, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils.encoding import smart_str
from django.utils.timezone import now
from loginas.utils import is_impersonated_session
from rest_framework import renderers, request, serializers, status, viewsets
from rest_framework.decorators import action, renderer_classes
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response

from posthog.api.routing import StructuredViewSetMixin
from posthog.models import Plugin, PluginAttachment, PluginConfig, User
from posthog.models.activity_logging.activity_log import (
    ActivityPage,
    Change,
    Detail,
    Trigger,
    dict_changes_between,
    load_all_activity,
    log_activity,
)
from posthog.models.activity_logging.activity_page import activity_page_response
from posthog.models.activity_logging.serializers import ActivityLogSerializer
from posthog.models.organization import Organization
from posthog.models.plugin import (
    PluginSourceFile,
    update_validated_data_from_url,
    validate_plugin_job_payload,
)
from posthog.models.utils import UUIDT, generate_random_token
from posthog.permissions import (
    OrganizationMemberPermissions,
    ProjectMembershipNecessaryPermissions,
    TeamMemberAccessPermission,
)
from posthog.plugins import can_configure_plugins, can_install_plugins, parse_url
from posthog.plugins.access import can_globally_manage_plugins
from posthog.queries.app_metrics.app_metrics import TeamPluginsDeliveryRateQuery
from posthog.redis import get_client
from posthog.utils import format_query_params_absolute_url


# Keep this in sync with: frontend/scenes/plugins/utils.ts
SECRET_FIELD_VALUE = "**************** POSTHOG SECRET FIELD ****************"


def _update_plugin_attachments(request: request.Request, plugin_config: PluginConfig):
    user = cast(User, request.user)
    for key, file in request.FILES.items():
        match = re.match(r"^add_attachment\[([^]]+)\]$", key)
        if match:
            _update_plugin_attachment(request, plugin_config, match.group(1), file, user)
    for key, _file in request.POST.items():
        match = re.match(r"^remove_attachment\[([^]]+)\]$", key)
        if match:
            _update_plugin_attachment(request, plugin_config, match.group(1), None, user)


def get_plugin_config_changes(old_config: Dict[str, Any], new_config: Dict[str, Any], secret_fields=[]) -> List[Change]:
    config_changes = dict_changes_between("Plugin", old_config, new_config)

    for i, change in enumerate(config_changes):
        if change.field in secret_fields:
            config_changes[i] = Change(
                type="PluginConfig",
                action=change.action,
                before=SECRET_FIELD_VALUE,
                after=SECRET_FIELD_VALUE,
            )

    return config_changes


def log_enabled_change_activity(
    new_plugin_config: PluginConfig, old_enabled: bool, user: User, was_impersonated: bool, changes=[]
):
    if old_enabled != new_plugin_config.enabled:
        log_activity(
            organization_id=new_plugin_config.team.organization.id,
            # Users in an org but not yet in a team can technically manage plugins via the API
            team_id=new_plugin_config.team.id,
            user=user,
            was_impersonated=was_impersonated,
            item_id=new_plugin_config.id,
            scope="PluginConfig",
            activity="enabled" if not old_enabled else "disabled",
            detail=Detail(name=new_plugin_config.plugin.name, changes=changes),
        )


def log_config_update_activity(
    new_plugin_config: PluginConfig,
    old_config: Dict[str, Any],
    secret_fields: Set[str],
    old_enabled: bool,
    user: User,
    was_impersonated: bool,
):
    config_changes = get_plugin_config_changes(
        old_config=old_config,
        new_config=new_plugin_config.config,
        secret_fields=secret_fields,
    )

    if len(config_changes) > 0:
        log_activity(
            organization_id=new_plugin_config.team.organization.id,
            # Users in an org but not yet in a team can technically manage plugins via the API
            team_id=new_plugin_config.team.id,
            user=user,
            was_impersonated=was_impersonated,
            item_id=new_plugin_config.id,
            scope="PluginConfig",
            activity="config_updated",
            detail=Detail(name=new_plugin_config.plugin.name, changes=config_changes),
        )

    log_enabled_change_activity(
        new_plugin_config=new_plugin_config, old_enabled=old_enabled, user=user, was_impersonated=was_impersonated
    )


def _update_plugin_attachment(
    request: request.Request, plugin_config: PluginConfig, key: str, file: Optional[UploadedFile], user: User
):
    try:
        plugin_attachment = PluginAttachment.objects.get(team=plugin_config.team, plugin_config=plugin_config, key=key)
        if file:
            activity = "attachment_updated"
            change = Change(
                type="PluginConfig",
                action="changed",
                before=plugin_attachment.file_name,
                after=file.name,
            )

            plugin_attachment.content_type = file.content_type
            plugin_attachment.file_name = file.name
            plugin_attachment.file_size = file.size
            plugin_attachment.contents = file.file.read()
            plugin_attachment.save()
        else:
            plugin_attachment.delete()

            activity = "attachment_deleted"
            change = Change(
                type="PluginConfig",
                action="deleted",
                before=plugin_attachment.file_name,
                after=None,
            )
    except ObjectDoesNotExist:
        if file:
            PluginAttachment.objects.create(
                team=plugin_config.team,
                plugin_config=plugin_config,
                key=key,
                content_type=str(file.content_type),
                file_name=file.name,
                file_size=file.size,
                contents=file.file.read(),
            )

            activity = "attachment_created"
            change = Change(type="PluginConfig", action="created", before=None, after=file.name)

    log_activity(
        organization_id=plugin_config.team.organization.id,
        team_id=plugin_config.team.id,
        user=user,
        was_impersonated=is_impersonated_session(request),
        item_id=plugin_config.id,
        scope="PluginConfig",
        activity=activity,
        detail=Detail(name=plugin_config.plugin.name, changes=[change]),
    )


# sending files via a multipart form puts the config JSON in a un-serialized format
def _fix_formdata_config_json(request: request.Request, validated_data: dict):
    if not validated_data.get("config", None) and cast(dict, request.POST).get("config", None):
        validated_data["config"] = json.loads(request.POST["config"])


def transpile(input_string: str, type: Literal["site", "frontend"] = "site") -> Optional[str]:
    from posthog.settings.base_variables import BASE_DIR

    transpiler_path = os.path.join(BASE_DIR, "plugin-transpiler/dist/index.js")
    if type not in ["site", "frontend"]:
        raise Exception('Invalid type. Must be "site" or "frontend".')

    process = subprocess.Popen(
        ["node", transpiler_path, "--type", type], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = process.communicate(input=input_string.encode())

    if process.returncode != 0:
        error = stderr.decode()
        raise Exception(error)
    return stdout.decode()


class PlainRenderer(renderers.BaseRenderer):
    format = "txt"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return smart_str(data, encoding=self.charset or "utf-8")


class PluginsAccessLevelPermission(BasePermission):
    message = "Your organization's plugin access level is insufficient."

    def has_permission(self, request, view) -> bool:
        min_level = (
            Organization.PluginsAccessLevel.CONFIG
            if request.method in SAFE_METHODS
            else Organization.PluginsAccessLevel.INSTALL
        )
        return view.organization.plugins_access_level >= min_level


class PluginOwnershipPermission(BasePermission):
    message = "This plugin installation is managed by another organization."

    def has_object_permission(self, request, view, object) -> bool:
        return view.organization == object.organization


class PluginSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    organization_name = serializers.SerializerMethodField()

    class Meta:
        model = Plugin
        fields = [
            "id",
            "plugin_type",
            "name",
            "description",
            "url",
            "icon",
            "config_schema",
            "tag",
            "latest_tag",
            "is_global",
            "organization_id",
            "organization_name",
            "capabilities",
            "metrics",
            "public_jobs",
        ]
        read_only_fields = ["id", "latest_tag"]

    def get_url(self, plugin: Plugin) -> Optional[str]:
        # remove ?private_token=... from url
        return str(plugin.url).split("?")[0] if plugin.url else None

    def get_latest_tag(self, plugin: Plugin) -> Optional[str]:
        if not plugin.latest_tag or not plugin.latest_tag_checked_at:
            return None

        if plugin.latest_tag != plugin.tag or plugin.latest_tag_checked_at > now() - relativedelta(seconds=60 * 30):
            return str(plugin.latest_tag)

        return None

    def get_organization_name(self, plugin: Plugin) -> str:
        return plugin.organization.name

    def create(self, validated_data: Dict, *args: Any, **kwargs: Any) -> Plugin:
        validated_data["url"] = self.initial_data.get("url", None)
        validated_data["organization_id"] = self.context["organization_id"]
        validated_data["updated_at"] = now()
        if validated_data.get("is_global") and not can_globally_manage_plugins(validated_data["organization_id"]):
            raise PermissionDenied("This organization can't manage global plugins!")

        plugin = Plugin.objects.install(**validated_data)

        return plugin

    def update(self, plugin: Plugin, validated_data: Dict, *args: Any, **kwargs: Any) -> Plugin:  # type: ignore
        context_organization = self.context["get_organization"]()
        if (
            "is_global" in validated_data
            and context_organization.plugins_access_level < Organization.PluginsAccessLevel.ROOT
        ):
            raise PermissionDenied("This organization can't manage global plugins!")
        validated_data["updated_at"] = now()
        return super().update(plugin, validated_data)


class PluginViewSet(StructuredViewSetMixin, viewsets.ModelViewSet):
    queryset = Plugin.objects.all()
    serializer_class = PluginSerializer
    permission_classes = [
        IsAuthenticated,
        ProjectMembershipNecessaryPermissions,
        OrganizationMemberPermissions,
        PluginsAccessLevelPermission,
        PluginOwnershipPermission,
    ]

    def get_queryset(self):
        queryset = super().get_queryset()
        queryset = queryset.select_related("organization")

        if self.action == "get" or self.action == "list":
            if can_install_plugins(self.organization) or can_configure_plugins(self.organization):
                return queryset
        else:
            if can_install_plugins(self.organization):
                return queryset
        return queryset.none()

    def get_plugin_with_permissions(self, reason="installation"):
        plugin = self.get_object()
        organization = self.organization
        if plugin.organization != organization:
            raise NotFound()
        if not can_install_plugins(self.organization):
            raise PermissionDenied(f"Plugin {reason} is not available for the current organization!")
        return plugin

    def filter_queryset_by_parents_lookups(self, queryset):
        try:
            return queryset.filter(
                Q(**self.parents_query_dict)
                | Q(is_global=True)
                | Q(
                    id__in=PluginConfig.objects.filter(  # If a config exists the org can see the plugin
                        team__organization_id=self.organization_id, deleted=False
                    ).values_list("plugin_id", flat=True)
                )
            )
        except ValueError:
            raise NotFound()

    @action(methods=["GET"], detail=False)
    def repository(self, request: request.Request, **kwargs):
        url = "https://raw.githubusercontent.com/PostHog/integrations-repository/main/plugins.json"
        plugins = requests.get(url)
        return Response(json.loads(plugins.text))

    @action(methods=["GET"], detail=False)
    def unused(self, request: request.Request, **kwargs):
        ids = Plugin.objects.exclude(
            id__in=PluginConfig.objects.filter(enabled=True).values_list("plugin_id", flat=True)
        ).values_list("id", flat=True)
        return Response(ids)

    @action(methods=["GET"], detail=False)
    def exports_unsubscribe_configs(self, request: request.Request, **kwargs):
        # return all the plugin_configs for the org that are not global transformation/filter plugins
        allowed_plugins_q = Q(plugin__is_global=True) & (
            Q(plugin__capabilities__methods__contains=["processEvent"]) | Q(plugin__capabilities={})
        )
        plugin_configs = PluginConfig.objects.filter(
            Q(team__organization_id=self.organization_id, enabled=True) & ~allowed_plugins_q
        )
        return Response(PluginConfigSerializer(plugin_configs, many=True).data)

    @action(methods=["GET"], detail=True)
    def check_for_updates(self, request: request.Request, **kwargs):
        plugin = self.get_plugin_with_permissions(reason="installation")
        latest_url = parse_url(plugin.url, get_latest_if_none=True)

        # use update to not trigger the post_save signal and avoid telling the plugin server to reload vms
        Plugin.objects.filter(id=plugin.id).update(
            latest_tag=latest_url.get("tag", latest_url.get("version", None)),
            latest_tag_checked_at=now(),
        )
        plugin.refresh_from_db()

        return Response({"plugin": PluginSerializer(plugin).data})

    @action(methods=["GET"], detail=True)
    def source(self, request: request.Request, **kwargs):
        plugin = self.get_plugin_with_permissions(reason="source editing")
        response: Dict[str, str] = {}
        for source in PluginSourceFile.objects.filter(plugin=plugin):
            response[source.filename] = source.source
        return Response(response)

    @action(methods=["PATCH"], detail=True)
    def update_source(self, request: request.Request, **kwargs):
        plugin = self.get_plugin_with_permissions(reason="source editing")
        sources: Dict[str, PluginSourceFile] = {}
        performed_changes = False
        for plugin_source_file in PluginSourceFile.objects.filter(plugin=plugin):
            sources[plugin_source_file.filename] = plugin_source_file
        for key, source in request.data.items():
            transpiled = None
            error = None
            status = None
            try:
                if key == "site.ts":
                    transpiled = transpile(source, type="site")
                    status = PluginSourceFile.Status.TRANSPILED
                elif key == "frontend.tsx":
                    transpiled = transpile(source, type="frontend")
                    status = PluginSourceFile.Status.TRANSPILED
            except Exception as e:
                error = str(e)
                status = PluginSourceFile.Status.ERROR

            if key not in sources:
                performed_changes = True
                sources[key], created = PluginSourceFile.objects.update_or_create(
                    plugin=plugin,
                    filename=key,
                    defaults={
                        "source": source,
                        "transpiled": transpiled,
                        "status": status,
                        "error": error,
                    },
                )
            elif sources[key].source != source or sources[key].transpiled != transpiled or sources[key].error != error:
                performed_changes = True
                if source is None:
                    sources[key].delete()
                    del sources[key]
                else:
                    sources[key].source = source
                    sources[key].transpiled = transpiled
                    sources[key].status = status
                    sources[key].error = error
                    sources[key].save()

        response: Dict[str, str] = {}
        for _, source in sources.items():
            response[source.filename] = source.source

        # Update values from plugin.json, if one exists
        if response.get("plugin.json"):
            plugin_json = json.loads(response["plugin.json"])
            if "name" in plugin_json and plugin_json["name"] != plugin.name:
                plugin.name = plugin_json.get("name")
                performed_changes = True
            if "config" in plugin_json and json.dumps(plugin_json["config"]) != json.dumps(plugin.config_schema):
                plugin.config_schema = plugin_json["config"]
                performed_changes = True

        # TODO: Truly deprecate this old field. Keeping the sync just in case for now.
        if response.get("index.ts") and plugin.source != response["index.ts"]:
            plugin.source = response["index.ts"]
            performed_changes = True

        # Save regardless if changed the plugin or plugin source models. This reloads the plugin in the plugin server.
        if performed_changes:
            plugin.updated_at = now()
            plugin.save()
        # Trigger capabilities update in plugin server, in case the app source changed the methods etc
        get_client().publish(
            "populate-plugin-capabilities",
            json.dumps({"plugin_id": str(plugin.id)}),
        )
        return Response(response)

    @action(methods=["POST"], detail=True)
    def upgrade(self, request: request.Request, **kwargs):
        plugin = self.get_plugin_with_permissions(reason="upgrading")
        serializer = PluginSerializer(plugin, context=self.get_serializer_context())
        if plugin.plugin_type not in (
            Plugin.PluginType.SOURCE,
            Plugin.PluginType.LOCAL,
        ):
            validated_data: Dict[str, Any] = {}
            plugin_json = update_validated_data_from_url(validated_data, plugin.url)
            with transaction.atomic():
                serializer.update(plugin, validated_data)
                PluginSourceFile.objects.sync_from_plugin_archive(plugin, plugin_json)
        return Response(serializer.data)

    def destroy(self, request: request.Request, *args, **kwargs) -> Response:
        instance = self.get_object()
        instance_id = instance.id
        if instance.is_global:
            raise ValidationError("This plugin is marked as global! Make it local before uninstallation")
        self.perform_destroy(instance)

        user = request.user

        log_activity(
            organization_id=instance.organization_id,
            # Users in an org but not yet in a team can technically manage plugins via the API
            team_id=user.team.id if user.team else 0,  # type: ignore
            user=user,  # type: ignore
            was_impersonated=is_impersonated_session(self.request),
            item_id=instance_id,
            scope="Plugin",
            activity="uninstalled",
            detail=Detail(name=instance.name),
        )

        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_create(self, serializer):
        serializer.save()

        user = serializer.context["request"].user

        log_activity(
            organization_id=serializer.instance.organization.id,
            # Users in an org but not yet in a team can technically manage plugins via the API
            team_id=user.team.id if user.team else 0,
            user=user,
            was_impersonated=is_impersonated_session(self.request),
            item_id=serializer.instance.id,
            scope="Plugin",
            activity="installed",
            detail=Detail(name=serializer.instance.name),
        )

    @action(methods=["GET"], url_path="activity", detail=False)
    def all_activity(self, request: request.Request, **kwargs):
        limit = int(request.query_params.get("limit", "10"))
        page = int(request.query_params.get("page", "1"))

        activity_page = load_all_activity(
            scope_list=["Plugin", "PluginConfig"],
            team_id=request.user.team.id,  # type: ignore
            limit=limit,
            page=page,
        )

        return activity_page_response(activity_page, limit, page, request)

    @staticmethod
    def _activity_page_response(
        activity_page: ActivityPage, limit: int, page: int, request: request.Request
    ) -> Response:
        return Response(
            {
                "results": ActivityLogSerializer(activity_page.results, many=True).data,
                "next": format_query_params_absolute_url(request, page + 1, limit, offset_alias="page")
                if activity_page.has_next
                else None,
                "previous": format_query_params_absolute_url(request, page - 1, limit, offset_alias="page")
                if activity_page.has_previous
                else None,
                "total_count": activity_page.total_count,
            },
            status=status.HTTP_200_OK,
        )


class PluginConfigSerializer(serializers.ModelSerializer):
    config = serializers.SerializerMethodField()
    plugin_info = serializers.SerializerMethodField()
    delivery_rate_24h = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()

    class Meta:
        model = PluginConfig
        fields = [
            "id",
            "plugin",  # TODO: Rename to plugin_id for consistency with team_id
            "enabled",
            "order",
            "config",
            "error",
            "team_id",
            "plugin_info",
            "delivery_rate_24h",
            "created_at",
            "updated_at",
            "name",
            "description",
            "deleted",
        ]
        read_only_fields = [
            "id",
            "team_id",
            "plugin_info",
            "error",
            "delivery_rate_24h",
            "created_at",
        ]

    def get_config(self, plugin_config: PluginConfig):
        attachments = PluginAttachment.objects.filter(plugin_config=plugin_config).only(
            "id", "file_size", "file_name", "content_type"
        )

        new_plugin_config = plugin_config.config.copy()

        secret_fields = _get_secret_fields_for_plugin(plugin_config.plugin)

        # do not send the real value to the client
        for key in secret_fields:
            if new_plugin_config.get(key):
                new_plugin_config[key] = SECRET_FIELD_VALUE

        for attachment in attachments:
            if attachment.key not in secret_fields:
                new_plugin_config[attachment.key] = {
                    "uid": attachment.id,
                    "saved": True,
                    "size": attachment.file_size,
                    "name": attachment.file_name,
                    "type": attachment.content_type,
                }
            else:
                new_plugin_config[attachment.key] = {
                    "uid": -1,
                    "saved": True,
                    "size": -1,
                    "name": SECRET_FIELD_VALUE,
                    "type": "application/octet-stream",
                }

        return new_plugin_config

    def to_representation(self, instance: Any) -> Any:
        representation = super().to_representation(instance)
        representation["name"] = representation["name"] or instance.plugin.name
        representation["description"] = representation["description"] or instance.plugin.description
        return representation

    def get_plugin_info(self, plugin_config: PluginConfig):
        if "view" in self.context and self.context["view"].action != "list":
            return PluginSerializer(instance=plugin_config.plugin).data
        else:
            return None

    def get_delivery_rate_24h(self, plugin_config: PluginConfig):
        if "delivery_rates_1d" in self.context:
            return self.context["delivery_rates_1d"].get(plugin_config.pk, None)
        else:
            return None

    def get_error(self, plugin_config: PluginConfig) -> None:
        # Reporting the single latest error is no longer supported: use app
        # metrics (for fatal errors) or plugin log entries (for all errors) for
        # error details instead.
        return None

    def create(self, validated_data: Dict, *args: Any, **kwargs: Any) -> PluginConfig:
        if not can_configure_plugins(self.context["get_organization"]()):
            raise ValidationError("Plugin configuration is not available for the current organization!")
        validated_data["team_id"] = self.context["team_id"]
        _fix_formdata_config_json(self.context["request"], validated_data)
        existing_config = PluginConfig.objects.filter(
            team_id=validated_data["team_id"], plugin_id=validated_data["plugin"]
        )
        if existing_config.exists():
            return self.update(existing_config.first(), validated_data)  # type: ignore

        validated_data["web_token"] = generate_random_token()
        plugin_config = super().create(validated_data)
        log_enabled_change_activity(
            new_plugin_config=plugin_config,
            old_enabled=False,
            changes=get_plugin_config_changes(
                old_config={},
                new_config=plugin_config.config,
                secret_fields=_get_secret_fields_for_plugin(plugin_config.plugin),
            ),
            user=self.context["request"].user,
            was_impersonated=is_impersonated_session(self.context["request"]),
        )

        _update_plugin_attachments(self.context["request"], plugin_config)
        return plugin_config

    def update(  # type: ignore
        self,
        plugin_config: PluginConfig,
        validated_data: Dict,
        *args: Any,
        **kwargs: Any,
    ) -> PluginConfig:
        _fix_formdata_config_json(self.context["request"], validated_data)
        validated_data.pop("plugin", None)
        # One can delete apps in the UI, plugin-server doesn't use that field
        # if deleted is set to true we always want to disable the app
        if "deleted" in validated_data and validated_data["deleted"] is True:
            validated_data["enabled"] = False

        # Keep old value for secret fields if no new value in the request
        secret_fields = _get_secret_fields_for_plugin(plugin_config.plugin)

        if "config" in validated_data:
            for key in secret_fields:
                if validated_data["config"].get(key) is None:  # explicitly checking None to allow ""
                    validated_data["config"][key] = plugin_config.config.get(key)

        old_config = plugin_config.config
        old_enabled = plugin_config.enabled
        response = super().update(plugin_config, validated_data)

        log_config_update_activity(
            new_plugin_config=plugin_config,
            old_config=old_config or {},
            old_enabled=old_enabled,
            secret_fields=secret_fields,
            user=self.context["request"].user,
            was_impersonated=is_impersonated_session(self.context["request"]),
        )

        _update_plugin_attachments(self.context["request"], plugin_config)
        return response


class PluginConfigViewSet(StructuredViewSetMixin, viewsets.ModelViewSet):
    queryset = PluginConfig.objects.all()
    serializer_class = PluginConfigSerializer
    permission_classes = [
        IsAuthenticated,
        ProjectMembershipNecessaryPermissions,
        OrganizationMemberPermissions,
        TeamMemberAccessPermission,
    ]

    def get_queryset(self):
        if not can_configure_plugins(self.team.organization_id):
            return self.queryset.none()
        queryset = super().get_queryset()
        if self.action == "list":
            queryset = queryset.filter(deleted=False)
        return queryset.order_by("order", "plugin_id")

    def get_serializer_context(self) -> Dict[str, Any]:
        context = super().get_serializer_context()
        if context["view"].action in ("retrieve", "list"):
            context["delivery_rates_1d"] = TeamPluginsDeliveryRateQuery(self.team).run()
        return context

    # we don't really use this endpoint, but have something anyway to prevent team leakage
    def destroy(self, request: request.Request, pk=None, **kwargs) -> Response:  # type: ignore
        if not can_configure_plugins(self.team.organization_id):
            return Response(status=404)
        plugin_config = PluginConfig.objects.get(team_id=self.team_id, pk=pk)
        plugin_config.enabled = False
        plugin_config.save()
        return Response(status=204)

    @action(methods=["PATCH"], detail=False)
    def rearrange(self, request: request.Request, **kwargs):
        if not can_configure_plugins(self.team.organization_id):
            raise ValidationError("Plugin configuration is not available for the current organization!")

        orders = request.data.get("orders", {})

        plugin_configs = PluginConfig.objects.filter(team_id=self.team.pk, enabled=True)
        plugin_configs_dict = {p.plugin_id: p for p in plugin_configs}
        for plugin_id, order in orders.items():
            plugin_config = plugin_configs_dict.get(int(plugin_id), None)
            if plugin_config and plugin_config.order != order:
                old_order = plugin_config.order
                plugin_config.order = order
                plugin_config.save()

                log_activity(
                    organization_id=self.organization.id,
                    # Users in an org but not yet in a team can technically manage plugins via the API
                    team_id=self.team.id,
                    user=request.user,  # type: ignore
                    was_impersonated=is_impersonated_session(self.request),
                    item_id=plugin_config.id,
                    scope="Plugin",  # use the type plugin so we can also provide unified history
                    activity="order_changed",
                    detail=Detail(
                        name=plugin_config.plugin.name,
                        changes=[
                            Change(
                                type="Plugin",
                                before=old_order,
                                after=order,
                                action="changed",
                                field="order",
                            )
                        ],
                    ),
                )

        return Response(PluginConfigSerializer(plugin_configs, many=True).data)

    @action(methods=["POST"], detail=True)
    def job(self, request: request.Request, **kwargs):
        if not can_configure_plugins(self.team.organization_id):
            raise ValidationError("Plugin configuration is not available for the current organization!")

        plugin_config = self.get_object()
        plugin_config_id = plugin_config.id
        job = request.data.get("job", {})

        if "type" not in job:
            raise ValidationError("The job type must be specified!")

        # job_type = job name
        job_type = job.get("type")
        job_payload = job.get("payload", {})
        job_op = job.get("operation", "start")
        job_id = str(UUIDT())

        validate_plugin_job_payload(
            plugin_config.plugin,
            job_type,
            job_payload,
            is_staff=request.user.is_staff or is_impersonated_session(request),
        )

        payload_json = json.dumps(
            {
                "type": job_type,
                "payload": {**job_payload, **{"$operation": job_op, "$job_id": job_id}},
                "pluginConfigId": plugin_config_id,
                "pluginConfigTeam": self.team.pk,
            }
        )
        sql = f"SELECT graphile_worker.add_job('pluginJob', %s)"
        params = [payload_json]
        try:
            connection = connections["graphile"] if "graphile" in connections else connections["default"]
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
        except Exception as e:
            raise Exception(f"Failed to execute postgres sql={sql},\nparams={params},\nexception={str(e)}")

        log_activity(
            organization_id=self.team.organization.id,
            # Users in an org but not yet in a team can technically manage plugins via the API
            team_id=self.team.pk,
            user=request.user,  # type: ignore
            was_impersonated=is_impersonated_session(self.request),
            item_id=plugin_config_id,
            scope="PluginConfig",  # use the type plugin so we can also provide unified history
            activity="job_triggered",
            detail=Detail(
                name=self.get_object().plugin.name,
                trigger=Trigger(job_type=job_type, job_id=job_id, payload=job_payload),
            ),
        )
        return Response(status=200)

    @action(methods=["GET"], detail=True)
    @renderer_classes((PlainRenderer,))
    def frontend(self, request: request.Request, **kwargs):
        plugin_config = self.get_object()
        plugin_source = PluginSourceFile.objects.filter(
            plugin_id=plugin_config.plugin_id, filename="frontend.tsx"
        ).first()
        if plugin_source and plugin_source.status == PluginSourceFile.Status.TRANSPILED:
            content = plugin_source.transpiled or ""
            return HttpResponse(content, content_type="application/javascript; charset=UTF-8")

        obj: Dict[str, Any] = {}
        if not plugin_source:
            obj = {"no_frontend": True}
        elif plugin_source.status is None or plugin_source.status == PluginSourceFile.Status.LOCKED:
            obj = {"transpiling": True}
        else:
            obj = {"error": plugin_source.error or "Error Compiling Plugin"}

        content = f"export function getFrontendApp () {'{'} return {json.dumps(obj)} {'}'}"
        return HttpResponse(content, content_type="application/javascript; charset=UTF-8")


def _get_secret_fields_for_plugin(plugin: Plugin) -> Set[str]:
    # A set of keys for config fields that have secret = true
    secret_fields = {field["key"] for field in plugin.config_schema if "secret" in field and field["secret"]}
    return secret_fields


class LegacyPluginConfigViewSet(PluginConfigViewSet):
    legacy_team_compatibility = True


class PipelineTransformationsViewSet(PluginViewSet):
    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(Q(capabilities__has_key="methods") & Q(capabilities__methods__contains=["processEvent"]))


class PipelineTransformationsConfigsViewSet(PluginConfigViewSet):
    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(
            Q(plugin__capabilities__has_key="methods") & Q(plugin__capabilities__methods__contains=["processEvent"])
        )


class PipelineDestinationsViewSet(PluginViewSet):
    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(
            Q(capabilities__has_key="methods")
            & (Q(capabilities__methods__contains=["onEvent"]) | Q(capabilities__methods__contains=["composeWebhook"]))
        )


class PipelineDestinationsConfigsViewSet(PluginConfigViewSet):
    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(
            Q(plugin__capabilities__has_key="methods")
            & (
                Q(plugin__capabilities__methods__contains=["onEvent"])
                | Q(plugin__capabilities__methods__contains=["composeWebhook"])
            )
        )

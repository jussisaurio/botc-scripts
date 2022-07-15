import json as js
import os
from tempfile import TemporaryFile

# Create your views here.
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.auth import logout
from django.db.models import Count
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import redirect
from django.views import generic
from django_filters.views import FilterView
from django_tables2.views import SingleTableMixin, SingleTableView
from django_tables2.config import RequestConfig
from django.shortcuts import render
from versionfield import Version

from scripts import filters, forms, models, script_json, tables, characters
from collections import Counter
from django.contrib.postgres.search import TrigramSimilarity

from typing import Dict, Any, List


class ScriptsListView(SingleTableMixin, FilterView):
    model = models.ScriptVersion
    template_name = "scriptlist.html"
    filterset_class = filters.ScriptVersionFilter
    table_pagination = {"per_page": 20}
    ordering = ["-pk"]
    script_view = None

    def get_filterset_kwargs(self, filterset_class):
        kwargs = super(ScriptsListView, self).get_filterset_kwargs(filterset_class)
        if kwargs["data"] is None:
            kwargs["data"] = {"latest": True}
        return kwargs

    def get_table_class(self):
        if self.request.user.is_authenticated:
            return tables.UserScriptTable
        return tables.ScriptTable


class UserScriptsListView(LoginRequiredMixin, SingleTableMixin, FilterView):
    model = models.ScriptVersion
    table_class = tables.UserScriptTable
    template_name = "scriptlist.html"
    script_view = None
    table_pagination = {"per_page": 20}
    ordering = ["-pk"]

    def get_filterset_class(self):
        if self.script_view == "favourite":
            return filters.ScriptVersionFilter
        return filters.FavouriteScriptVersionFilter

    def get_queryset(self):
        queryset = super(UserScriptsListView, self).get_queryset()
        if self.script_view == "favourite":
            queryset = queryset.filter(favourites__user=self.request.user)
        elif self.script_view == "owned":
            queryset = queryset.filter(script__owner=self.request.user)
        return queryset

    def get_filterset_kwargs(self, filterset_class):
        kwargs = super(UserScriptsListView, self).get_filterset_kwargs(filterset_class)
        if kwargs["data"] is None:
            kwargs["data"] = {"latest": True}
        return kwargs


def get_json_additions(old_json, new_json):
    for old_id in old_json:
        if old_id["id"] == "_meta":
            continue
        for new_id in new_json:
            if new_id["id"] == "_meta":
                continue

            if old_id == new_id:
                new_json.remove(new_id)

    for new_id in new_json:
        if new_id["id"] == "_meta":
            new_json.remove(new_id)
            break

    return new_json


def get_similarity(json1: Dict, json2: Dict, same_type: bool) -> int:
    similarity = 0
    similarity_max = max(len(json1), len(json2))
    similarity_min = min(len(json1), len(json2))
    id2_meta = 0
    for id in json1:
        if id["id"] == "_meta":
            similarity_max = min(similarity_max, len(json1) - 1)
            similarity_min = min(similarity_min, len(json1) - 1)
            continue
        for id2 in json2:
            if id2["id"] == "_meta":
                similarity_max = min(similarity_max, len(json2) - 1)
                similarity_min = min(similarity_min, len(json2) - 1)
                id2_meta = 1
                continue
            if id["id"] == id2["id"]:
                similarity += 1
                break

    similarity_comp = similarity_max if same_type else similarity_min

    return round((similarity / similarity_comp) * 100)


def get_comment_data(comment: models.Comment, indent: int) -> List:
    data = []
    comment_data = {}
    comment_data["comment"] = comment
    comment_data["indent"] = indent
    data.append(comment_data)
    for child_comment in comment.children.all().order_by("created"):
        data.extend(get_comment_data(child_comment, min(indent + 1, 6)))
    return data


def get_comments(script: models.Script) -> Dict:
    comments = []
    for comment in script.comments.filter(parent__isnull=True).order_by("created"):
        comments.extend(get_comment_data(comment, 0))
    return comments


def count_character(
    script_content: Dict, character_type: characters.CharacterType
) -> int:
    count = 0
    for json_entry in script_content:
        character = characters.Character.get(json_entry.get("id"))
        if character and character.character_type == character_type:
            count += 1
    return count


class ScriptView(generic.DetailView):
    template_name = "script.html"
    model = models.Script

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        context["active-tab"] = ""
        _messages = messages.get_messages(request)
        for message in _messages:
            context["activetab"] = message.message
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if "selected_version" in self.request.GET:
            current_script = self.object.versions.get(
                version=self.request.GET["selected_version"]
            )
        elif "version" in self.kwargs:
            current_script = self.object.versions.get(version=self.kwargs["version"])
        else:
            current_script = self.object.versions.order_by("-version").first()
        context["script_version"] = current_script

        changes = {}
        diff_script_version = None
        for script_version in self.object.versions.all().order_by("-version"):

            # If we're looking at an older script, don't show changes future to that.
            if script_version.version > current_script.version:
                continue

            if diff_script_version:
                changes[diff_script_version.version] = {}
                changes[diff_script_version.version]["additions"] = get_json_additions(
                    script_version.content.copy(), diff_script_version.content.copy()
                )
                changes[diff_script_version.version]["deletions"] = get_json_additions(
                    diff_script_version.content.copy(), script_version.content.copy()
                )
                changes[diff_script_version.version][
                    "previous_version"
                ] = script_version.version
            diff_script_version = script_version

        context["changes"] = changes

        similarity = {}
        similarity[models.ScriptTypes.TEENSYVILLE.value] = {}
        similarity[models.ScriptTypes.FULL.value] = {}
        for script_version in models.ScriptVersion.objects.filter(latest=True).order_by(
            "pk"
        ):
            if current_script == script_version:
                continue

            similarity[script_version.script_type][script_version] = get_similarity(
                current_script.content,
                script_version.content,
                current_script.script_type == script_version.script_type,
            )
        context["similarity"] = {}
        context["similarity"][models.ScriptTypes.TEENSYVILLE.value] = sorted(
            similarity[models.ScriptTypes.TEENSYVILLE].items(),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
        context["similarity"][models.ScriptTypes.FULL.value] = sorted(
            similarity[models.ScriptTypes.FULL].items(),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
        context["script_version"] = current_script
        context["comments"] = get_comments(current_script.script)

        return context


def update_script(script_version, cleaned_data, author):
    script_version.script_type = cleaned_data["script_type"]
    script_version.author = author
    if cleaned_data.get("notes", None):
        script_version.notes = cleaned_data["notes"]
    if cleaned_data.get("pdf", None):
        script_version.pdf = cleaned_data["pdf"]
    script_version.tags.set(cleaned_data["tags"])
    script_version.save()


class ScriptUploadView(generic.FormView):
    template_name = "upload.html"
    form_class = forms.ScriptForm
    script_version = None

    def get_initial(self):
        initial = super().get_initial()
        script_pk = self.request.GET.get("script", None)
        if script_pk:
            script = models.Script.objects.get(pk=script_pk)
            if script:
                script_version = script.latest_version()
                initial["name"] = script.name
                initial["author"] = script_version.author
                initial["version"] = script_version.version
                if script_version.notes:
                    initial["notes"] = script_version.notes
        return initial

    def get_form_kwargs(self):
        """
        We want to provide the user to the Form's clean method so the user
        can't overwrite scripts owned by someone else.
        """
        kwargs = super(ScriptUploadView, self).get_form_kwargs()
        kwargs.update({"user": self.request.user})
        return kwargs

    def get_form(self):
        """
        If this is an anonymous user, don't allow them to add notes.
        """
        form = super().get_form(self.form_class)

        # If this user isn't authenticated, do allow them to add notes or upload anonymously
        # (since it will be anonymous anyway).
        if not self.request.user.is_authenticated:
            form.fields.pop("notes")
            form.fields.pop("anonymous")
        else:
            script_pk = self.request.GET.get("script", None)
            if script_pk:
                script = models.Script.objects.get(pk=script_pk)

                # If this script is owned by someone, and it is not the currently logged in
                # user, don't show the notes field.
                if script and script.owner and (script.owner != self.request.user):
                    form.fields.pop("notes")

                # If this is an update to an existing script, we can't make it anonymous.
                if script:
                    form.fields.pop("anonymous")

        return form

    def get_success_url(self):
        return "/script/" + str(self.script_version.script.pk)

    def form_valid(self, form):
        user = self.request.user
        json = forms.get_json_content(form.cleaned_data)
        is_latest = True

        # Use the script name from the JSON in preference of the text field.
        script_name = script_json.get_name_from_json(json)
        if not script_name:
            script_name = form.cleaned_data["name"]

        # Either get the current script, or create a new one based on the name.
        script, created = models.Script.objects.get_or_create(name=script_name)

        # We only want to set the owner on newly created scripts, so if we've
        # just created the script and the user is authenticated, set the owner to this user
        # unless they uploaded anonymously.
        # It's possible the user is uploading a "new" script by uploading a new version
        # of an existing script with a new name. In this instance, the anonymous field
        # isn't present, so default that this is anonymous.
        if (
            created
            and user.is_authenticated
            and not form.cleaned_data.get("anonymous", True)
        ):
            script.owner = user
            script.save()

        # Use the author in the JSON in preference of the text field.
        author = script_json.get_author_from_json(json)
        if not author:
            author = form.cleaned_data["author"]

        # The user may just be updating some info about an existing script, so let them
        # do that. The form validation should catch that the JSON content is the same.
        if not created:
            try:
                script_version = models.ScriptVersion.objects.get(
                    script=script, version=form.cleaned_data["version"]
                )
                # We're updating an existing entry.
                # Our validation should have caught not being able to upload different
                # JSON content for this script.
                update_script(script_version, form.cleaned_data, author)
                self.script_version = script_version
                return super().form_valid(form)
            except models.ScriptVersion.DoesNotExist:
                # This is not an existing version.
                if script.latest_version().content == json:
                    # The content hasn't change from the latest version, so just update
                    # that.
                    update_script(script.latest_version(), form.cleaned_data, author)
                    self.script_version = script.latest_version()
                    return super().form_valid(form)
                if (
                    Version(form.cleaned_data["version"])
                    > script.latest_version().version
                ):
                    # This is newer than the latest version, so set that
                    # version to not be latest.
                    latest_version = script.latest_version()
                    latest_version.latest = False
                    latest_version.save()
                else:
                    # We're uploading an older version, so don't mark this version
                    # as the latest, that's still the current latest.
                    is_latest = False

        num_townsfolk = count_character(json, characters.CharacterType.TOWNSFOLK)
        num_outsiders = count_character(json, characters.CharacterType.OUTSIDER)
        num_minions = count_character(json, characters.CharacterType.MINION)
        num_demons = count_character(json, characters.CharacterType.DEMON)
        num_fabled = count_character(json, characters.CharacterType.FABLED)

        # Create the Script Version object from the form.
        if form.cleaned_data.get("notes", None):
            self.script_version = models.ScriptVersion.objects.create(
                version=form.cleaned_data["version"],
                script_type=form.cleaned_data["script_type"],
                content=json,
                script=script,
                pdf=form.cleaned_data["pdf"],
                author=author,
                notes=form.cleaned_data["notes"],
                latest=is_latest,
                num_townsfolk=num_townsfolk,
                num_outsiders=num_outsiders,
                num_minions=num_minions,
                num_demons=num_demons,
                num_fabled=num_fabled,
            )
        else:
            self.script_version = models.ScriptVersion.objects.create(
                version=form.cleaned_data["version"],
                script_type=form.cleaned_data["script_type"],
                content=json,
                script=script,
                pdf=form.cleaned_data["pdf"],
                author=author,
                latest=is_latest,
                num_townsfolk=num_townsfolk,
                num_outsiders=num_outsiders,
                num_minions=num_minions,
                num_demons=num_demons,
                num_fabled=num_fabled,
            )
        self.script_version.tags.set(form.cleaned_data["tags"])

        return super().form_valid(form)


class StatisticsView(generic.TemplateView):
    template_name = "statistics.html"

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        stats_character = None
        characters_to_display = 5

        if "all" in self.request.GET:
            queryset = models.ScriptVersion.objects.all()
        else:
            queryset = models.ScriptVersion.objects.filter(latest=True)

        if "character" in kwargs:
            if characters.Character.get(kwargs.get("character")):
                stats_character = characters.Character.get(kwargs.get("character"))
                queryset = queryset.filter(
                    content__contains=[{"id": stats_character.json_id}]
                )
            else:
                raise Http404()
        elif "tags" in kwargs:
            tags = models.ScriptTag.objects.get(pk=kwargs.get("tags"))
            if tags:
                queryset = models.ScriptVersion.objects.filter(tags__in=[tags])

        if "tags" in self.request.GET:
            try:
                int(self.request.GET.get("tags"))
                tags = models.ScriptTag.objects.get(pk=self.request.GET.get("tags"))
                if tags:
                    queryset = queryset.filter(tags__in=[tags])
            except ValueError:
                pass

        if "num" in self.request.GET:
            try:
                int(self.request.GET.get("num"))
                if int(self.request.GET.get("num")):
                    characters_to_display = int(self.request.GET.get("num"))
                    if characters_to_display < 1:
                        characters_to_display = 5
            except ValueError:
                pass

        if "edition" in self.request.GET:
            try:
                edition = models.Edition(self.request.GET.get("edition"))
                queryset = filters.filter_by_edition(queryset, edition)
            except ValueError:
                pass

        context["total"] = queryset.count()

        character_count = {}
        for type in characters.CharacterType:
            character_count[type.value] = Counter()
        for character in characters.Character:
            # If we're on a Character Statistics page, don't include this character in the count.
            if character == stats_character:
                continue

            character_count[character.character_type.value][
                character
            ] = queryset.filter(content__contains=[{"id": character.json_id}]).count()

        for type in characters.CharacterType:
            context[type.value] = character_count[type.value].most_common(
                characters_to_display
            )
            context[type.value + "least"] = character_count[type.value].most_common()[
                : ((characters_to_display + 1) * -1) : -1
            ]

        return context


class UserDeleteView(LoginRequiredMixin, generic.TemplateView):
    """
    Deletes the currently signed-in user.
    """

    template_name = "account/delete.html"

    def post(self, request):
        user = request.user
        logout(request)
        user.delete()
        return HttpResponseRedirect("/")


def get_script_version(request, pk: int, version: str) -> models.ScriptVersion:
    try:
        script = models.Script.objects.get(pk=pk)
        script_version = script.versions.get(version=version)
    except (models.Script.DoesNotExist, models.ScriptVersion.DoesNotExist):
        return redirect(request.POST["next"])
    return script_version


def update_user_related_script(model, user: User, script: models.ScriptVersion) -> None:
    if user.is_authenticated:
        if model.objects.filter(user=user, script=script).exists():
            object = model.objects.get(user=user, script=script)
            object.delete()
        else:
            model.objects.create(user=user, script=script)


def vote_for_script(request, pk: int, version: str) -> None:
    if request.method != "POST":
        raise Http404()
    script_version = get_script_version(request, pk, version)
    update_user_related_script(models.Vote, request.user, script_version)
    return redirect(request.POST["next"])


def favourite_script(request, pk: int, version: str) -> None:
    if request.method != "POST":
        raise Http404()
    script_version = get_script_version(request, pk, version)
    update_user_related_script(models.Favourite, request.user, script_version)
    return redirect(request.POST["next"])


def download_json(request, pk: int, version: str) -> FileResponse:
    script = models.Script.objects.get(pk=pk)
    script_version = script.versions.get(version=version)
    json_content = js.JSONEncoder().encode(script_version.content)
    temp_file = TemporaryFile()
    temp_file.write(json_content.encode("utf-8"))
    temp_file.flush()
    temp_file.seek(0)
    response = FileResponse(
        temp_file, as_attachment=True, filename=(script.name + ".json")
    )
    return response


def download_pdf(request, pk: int, version: str) -> FileResponse:
    script = models.Script.objects.get(pk=pk)
    script_version = script.versions.get(version=version)
    if os.environ.get("DJANGO_HOST", None):
        return FileResponse(script_version.pdf, as_attachment=True)
    else:
        return FileResponse(open(script_version.pdf.name, "rb"), as_attachment=True)


class CollectionScriptListView(SingleTableView):
    model = models.ScriptVersion
    template_name = "collection.html"
    table_pagination = {"per_page": 20}
    ordering = ["pk"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["collection"] = models.Collection.objects.get(pk=self.kwargs["pk"])
        return context

    def get_table_class(self):
        collection = models.Collection.objects.get(pk=self.kwargs["pk"])
        if self.request.user == collection.owner:
            return tables.CollectionScriptTable
        elif self.request.user.is_authenticated:
            return tables.UserScriptTable
        return tables.ScriptTable

    def get_queryset(self):
        collection = models.Collection.objects.get(pk=self.kwargs["pk"])
        return collection.scripts.order_by("pk")


class CollectionListView(SingleTableMixin, FilterView):
    model = models.Collection
    template_name = "collection_list.html"
    table_pagination = {"per_page": 20}
    ordering = ["pk"]
    table_class = tables.CollectionTable
    filterset_class = filters.CollectionFilter


class CollectionCreateView(LoginRequiredMixin, generic.edit.CreateView):
    template_name = "upload.html"
    form_class = forms.CollectionForm
    model = models.Collection

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super().form_valid(form)

    def get_success_url(self) -> str:
        return "/collection/" + str(self.object.id)


class CollectionEditView(LoginRequiredMixin, generic.edit.UpdateView):
    template_name = "upload.html"
    form_class = forms.CollectionForm
    model = models.Collection

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super().form_valid(form)

    def get_success_url(self) -> str:
        return "/collection/" + str(self.object.id)

    def get(self, request, *args, **kwargs):
        """
        A user should only be able to edit the collections they own.
        """
        self.object = self.get_object()
        if self.object.owner != self.request.user:
            raise Http404("Cannot edit a collection you don't own.")
        return super(CollectionEditView, self).get(request, *args, **kwargs)


class CollectionDeleteView(LoginRequiredMixin, generic.edit.BaseDeleteView):
    """
    Deletes a collection.
    """

    model = models.Collection

    def get_success_url(self) -> str:
        return "/"

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.owner != request.user:
            raise Http404("Cannot delete a collection you don't own.")
        success_url = self.get_success_url()
        self.object.delete()
        return HttpResponseRedirect(success_url)


class AddScriptToCollectionView(LoginRequiredMixin, generic.View):
    """
    Adds a script to a collection.
    """

    def post(self, request, *args, **kwargs):
        collection = models.Collection.objects.get(pk=request.POST.get("collection"))
        script = models.ScriptVersion.objects.get(pk=request.POST.get("script_version"))
        collection.scripts.add(script)
        return HttpResponseRedirect(
            "/script/" + str(script.script.pk) + "/" + str(script.version)
        )


class RemoveScriptFromCollectionView(LoginRequiredMixin, generic.View):
    """
    Removes a script from a collection.
    """

    def post(self, request, *args, **kwargs):
        try:
            collection = models.Collection.objects.get(pk=kwargs["collection"])
        except models.Collection.DoesNotExist:
            raise Http404("Unknown collection.")

        if collection.owner != self.request.user:
            raise Http404("Cannot edit a collection you don't own.")

        try:
            script = models.ScriptVersion.objects.get(pk=kwargs["script"])
        except models.ScriptVersion.DoesNotExist:
            raise Http404("Unknown script.")

        collection.scripts.remove(script)
        return HttpResponseRedirect(f"/collection/{collection.pk}")


class CommentCreateView(LoginRequiredMixin, generic.View):
    """
    Creates comments on scripts.
    """

    def post(self, request, *args, **kwargs):
        parent = None
        try:
            script = models.Script.objects.get(pk=request.POST["script"])
        except models.Script.DoesNotExist:
            return HttpResponseRedirect("/")

        if request.POST.get("parent"):
            try:
                parent = models.Comment.objects.get(pk=request.POST["parent"])
            except models.Comment.DoesNotExist:
                return HttpResponseRedirect(f"/script/{script.pk}")

        if request.POST["comment"]:
            if parent:
                models.Comment.objects.create(
                    user=request.user,
                    comment=request.POST["comment"],
                    script=script,
                    parent=parent,
                )
            else:
                models.Comment.objects.create(
                    user=request.user, comment=request.POST["comment"], script=script
                )
        messages.success(request, "comments-tab")
        return HttpResponseRedirect(f"/script/{script.pk}")


class CommentEditView(LoginRequiredMixin, generic.View):
    """
    Edits a comment.
    """

    def post(self, request, *args, **kwargs):
        if request.POST["comment"]:
            comment = models.Comment.objects.get(pk=kwargs["pk"])

            if comment.user != request.user:
                raise Http404("Cannot edit a comment you did not create.")

            comment.comment = request.POST["comment"]
            comment.save()

        messages.success(request, "comments-tab")
        success_url = f"/script/{comment.script.pk}"
        return HttpResponseRedirect(success_url)


class CommentDeleteView(LoginRequiredMixin, generic.View):
    """
    Removes a script from a collection.
    """

    def post(self, request, *args, **kwargs):
        comment = models.Comment.objects.get(pk=kwargs["pk"])

        if comment.user != request.user:
            raise Http404("Cannot delete a comment you did not make.")

        if comment.parent:
            # If this comment has a parent then update all our child comments to point
            # to that same parent.
            for child in comment.children.all():
                child.parent = comment.parent
                child.save()

        success_url = f"/script/{comment.script.pk}"
        comment.delete()
        messages.success(request, "comments-tab")
        return HttpResponseRedirect(success_url)


class AdvancedSearchView(generic.FormView, SingleTableMixin):
    template_name = "advanced_search.html"
    form_class = forms.AdvancedSearchForm
    script_version = None

    def form_valid(self, form):
        queryset = models.ScriptVersion.objects.all()
        if form.cleaned_data.get("name"):
            queryset = queryset.annotate(
                name_similarity=TrigramSimilarity(
                    "script__name", form.cleaned_data.get("name")
                )
            )
            queryset = queryset.filter(name_similarity__gt=0).order_by(
                "-name_similarity"
            )
        if form.cleaned_data.get("author"):
            queryset = queryset.annotate(
                author_similarity=TrigramSimilarity(
                    "author", form.cleaned_data.get("author")
                )
            )
            queryset = queryset.filter(author_similarity__gt=0).order_by(
                "-author_similarity"
            )

        if form.cleaned_data.get("includes_characters"):
            queryset = filters.include_characters(
                queryset, form.cleaned_data.get("includes_characters")
            )
        if form.cleaned_data.get("excludes_characters"):
            queryset = filters.exclude_characters(
                queryset, form.cleaned_data.get("excludes_characters")
            )

        queryset = filters.filter_by_edition(queryset, form.cleaned_data.get("edition"))
        for tag in form.cleaned_data.get("tags"):
            queryset = queryset.filter(tags=tag)

        # These probably don't work, because we're filtering on a single value each time so if more than
        # one value is selected, they're filter each other out.
        for number in form.cleaned_data.get("number_of_townsfolk"):
            queryset = queryset.filter(num_townsfolk=number)

        for number in form.cleaned_data.get("number_of_outsiders"):
            queryset = queryset.filter(num_outsider=number)

        for number in form.cleaned_data.get("number_of_minions"):
            queryset = queryset.filter(num_minions=number)

        for number in form.cleaned_data.get("number_of_demons"):
            queryset = queryset.filter(num_demons=number)

        for number in form.cleaned_data.get("number_of_fabled"):
            queryset = queryset.filter(num_fabled=number)

        if form.cleaned_data.get("minimum_number_of_likes"):
            queryset = queryset.annotate(score=Count("votes"))
            queryset = queryset.filter(
                score__gte=form.cleaned_data.get("minimum_number_of_likes")
            )

        if form.cleaned_data.get("minimum_number_of_favourites"):
            queryset = queryset.annotate(num_favs=Count("favourites"))
            queryset = queryset.filter(
                num_favs__gte=form.cleaned_data.get("minimum_number_of_favourites")
            )

        if form.cleaned_data.get("minimum_number_of_comments"):
            queryset = queryset.annotate(num_comments=Count("script__comments"))
            queryset = queryset.filter(
                num_comments__gte=form.cleaned_data.get("minimum_number_of_comments")
            )

        context = {}
        table = tables.ScriptTable(queryset[:20])
        context["table"] = table
        return render(self.request, "scriptlist.html", context)

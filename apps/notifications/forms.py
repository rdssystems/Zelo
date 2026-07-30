from django import forms


class AnnouncementForm(forms.Form):
    title = forms.CharField(max_length=200, label="Título")
    message = forms.CharField(label="Mensagem", widget=forms.Textarea)

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("O título é obrigatório.")
        return title

    def clean_message(self):
        message = self.cleaned_data["message"].strip()
        if not message:
            raise forms.ValidationError("A mensagem é obrigatória.")
        return message

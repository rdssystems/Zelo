from .services import theme_from_host


def entrance_theme(request):
    """Tema (barbearia/salão) das telas de entrada — login, cadastro, reset
    de senha, confirmação de e-mail. Precisa ser um context processor (não
    algo setado view a view) porque parte dessas telas são views prontas do
    django-allauth (`PasswordResetView`, `ConfirmEmailView`...), onde não dá
    pra injetar contexto customizado sem sobrescrever a view inteira."""
    theme = theme_from_host(request.get_host())
    return {
        "entrance_theme": theme,
        "entrance_base": (
            "account/_base_barbearia.html" if theme == "barbearia" else "account/_base_salao.html"
        ),
    }

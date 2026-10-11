"""Offline help. Each control maps to a stable topic; no remote HTML or scripts. The few addresses it
names open in the browser when clicked."""
from html import escape
import math
import re

from PyQt5.QtCore import QAbstractAnimation, QRectF, Qt, QVariantAnimation
from PyQt5.QtGui import QColor, QDesktopServices, QImage, QPainter, QTextDocument
from PyQt5.QtWidgets import QDialog, QPushButton, QTextBrowser, QVBoxLayout

from . import theme
from .pictures import TAKEOUT_REQUEST


LANGUAGES = ("en", "ru", "es")
TITLES = ("Help & FAQ", "Помощь и FAQ", "Ayuda y preguntas frecuentes")
# Entries: topic -> ((English question, answer), (Russian...), (Spanish...)). An answer is a paragraph,
# or a tuple of steps shown as a numbered list.
# Written for someone who has never exported a chat: what they want to know, in a few plain
# sentences, and nothing that raises a worry the answer does not settle.
TOPICS = {
    # Only in a copy that asks for an API ID and API Hash: it has none, or Telegram refused its own.
    "configuration": (
        ("Why does the app ask for an API ID and API Hash?", "Every Telegram app connects with an API ID and API Hash of its own. This copy has none, or Telegram no longer accepts them. Download the latest version from savegram.tech, or get your own in a few minutes as the next answer shows: the app keeps them on this computer only."),
        ("Почему программа просит API ID и API Hash?", "Каждое приложение Telegram подключается со своими API ID и API Hash. В этой копии программы их нет, или Telegram их больше не принимает. Скачайте свежую версию с savegram.tech или за несколько минут получите свои, как сказано в следующем ответе: программа хранит их только на этом компьютере."),
        ("¿Por qué la aplicación pide un API ID y un API Hash?", "Cada aplicación de Telegram se conecta con su propio API ID y API Hash. Esta copia no los tiene, o Telegram ya no los acepta. Descarga la última versión desde savegram.tech o consigue los tuyos en unos minutos, como explica la siguiente respuesta: la aplicación los guarda solo en este equipo.")),
    "app_keys": (
        ("How do I get my own API ID and API Hash?", (
            "Open my.telegram.org and sign in with your Telegram phone number; the code comes in Telegram.",
            "Click “API development tools”.",
            "Fill in “App title” with any name and “Short name” with 5 to 32 Latin letters or digits, choose “Desktop” and click “Create application”.",
            "Copy “App api_id” into the API ID field and “App api_hash” into the API Hash field, then click “Sign in with QR”.")),
        ("Как получить свои API ID и API Hash?", (
            "Откройте my.telegram.org и войдите по номеру телефона Telegram — код придёт в Telegram.",
            "Нажмите «API development tools».",
            "В «App title» впишите любое название, в «Short name» — от 5 до 32 латинских букв или цифр, выберите «Desktop» и нажмите «Create application».",
            "Скопируйте «App api_id» в поле API ID, а «App api_hash» — в поле API Hash и нажмите «Войти по QR».")),
        ("¿Cómo consigo mi propio API ID y API Hash?", (
            "Abre my.telegram.org e inicia sesión con tu número de Telegram; el código llega por Telegram.",
            "Pulsa «API development tools».",
            "En «App title» escribe cualquier nombre y en «Short name» de 5 a 32 letras latinas o cifras, elige «Desktop» y pulsa «Create application».",
            "Copia «App api_id» en el campo API ID y «App api_hash» en el campo API Hash, y pulsa «Entrar con QR»."))),
    "login": (
        ("How do I sign in?", "Click “Sign in with QR”. On your phone, open Telegram → Settings → Devices → Link Desktop Device and point the camera at the code. Signing in lets the app read the chat you choose."),
        ("Как войти?", "Нажмите «Войти по QR». На телефоне откройте Telegram → Настройки → Устройства → Подключить устройство и наведите камеру на код. Вход нужен, чтобы программа могла прочитать выбранный вами чат."),
        ("¿Cómo inicio sesión?", "Pulsa «Entrar con QR». En el teléfono, abre Telegram → Ajustes → Dispositivos → Vincular dispositivo y apunta la cámara al código. Iniciar sesión permite a la aplicación leer el chat que elijas.")),
    "twofa": (
        ("Why am I asked for a password after the QR code?", "Your Telegram account has a cloud password (two-step verification). Enter the same password you use in Telegram. The app does not save it. If you forgot it, reset it in Telegram."),
        ("Почему после QR-кода спрашивают пароль?", "В вашем Telegram включён облачный пароль (двухэтапная аутентификация). Введите тот же пароль, что и в Telegram. Программа его не сохраняет. Если вы его забыли, восстановите его в Telegram."),
        ("¿Por qué me pide una contraseña después del código QR?", "Tu cuenta de Telegram tiene contraseña en la nube (verificación en dos pasos). Escribe la misma contraseña que usas en Telegram. La aplicación no la guarda. Si la olvidaste, recupérala en Telegram.")),
    "remember": (
        ("What does “Remember on this computer” do?", "Next time the app opens already signed in, without a QR code, and large chats export faster. Turn it on only on your own computer. When it is off, the app signs out when you close it."),
        ("Что даёт «Запомнить на этом компьютере»?", "В следующий раз программа откроется уже с входом, без QR-кода, а большие чаты будут выгружаться быстрее. Включайте только на своём компьютере. Если не включать, программа выходит из аккаунта при закрытии."),
        ("¿Qué hace «Recordar en este equipo»?", "La próxima vez la aplicación se abrirá con la sesión iniciada, sin código QR, y los chats grandes se exportarán más rápido. Actívalo solo en tu propio equipo. Si está desactivado, la aplicación cierra la sesión al salir.")),
    "chats": (
        ("How do I choose a chat?", "Start typing its name in the “Chat” field: the chats that match appear, whatever part of the name you type. You can also pick it from the list. Loading the list does not export anything."),
        ("Как выбрать чат?", "Начните вводить название в поле «Чат» — появятся подходящие чаты, по любой части названия. Можно и выбрать в списке. Загрузка списка ничего не выгружает."),
        ("¿Cómo elijo un chat?", "Empieza a escribir su nombre en el campo «Chat»: aparecerán los chats que coinciden, con cualquier parte del nombre. También puedes elegirlo en la lista. Cargar la lista no exporta nada.")),
    "dates": (
        ("What goes into “From” and “To”?", "The period of messages you need; both ends are included. Times are your computer's. To include the whole last day, set 23:59:59. To export the entire history, leave the start date as it is."),
        ("Что указывать в «С» и «По»?", "Период, за который нужны сообщения; обе даты включаются. Время — по часам вашего компьютера. Чтобы захватить последний день целиком, поставьте 23:59:59. Для всей истории оставьте начальную дату как есть."),
        ("¿Qué pongo en «Desde» y «Hasta»?", "El periodo de mensajes que necesitas; ambos extremos se incluyen. La hora es la de tu equipo. Para incluir el último día entero, pon 23:59:59. Para exportar todo el historial, deja la fecha inicial como está.")),
    "media": (
        ("What should I choose under “Media”?", "“All media” saves the messages with all their photos, videos and files. “Text, avatars and custom emoji” leaves attachments out: the archive is smaller and ready sooner. “Text only, no files” downloads no files at all."),
        ("Что выбрать в поле «Медиа»?", "«Все медиа» — сообщения со всеми фото, видео и файлами. «Текст, аватары и кастомные эмодзи» — без вложений: архив меньше и готов быстрее. «Только текст, без файлов» — совсем без файлов."),
        ("¿Qué elijo en «Archivos»?", "«Todos los archivos» guarda los mensajes con todas sus fotos, vídeos y archivos. «Texto, avatares y emojis personalizados» deja fuera los adjuntos: la exportación ocupa menos y está lista antes. «Solo texto, sin archivos» no descarga ningún archivo.")),
    "anonymize_exporter": (
        ("What does “Hide who exported the chat” do?", "It removes from the archive what points to your account: your reactions and poll votes, your read marks, and by default people's names and photos, which are replaced by @username or a number. Messages, files and dates stay, so in a private chat or Saved Messages it is still clear who made the export."),
        ("Что делает «Скрыть автора экспорта»?", "Убирает из архива то, что указывает на ваш аккаунт: ваши реакции и голоса в опросах, отметки о прочтении, а также, по умолчанию, имена и фото участников — вместо них будут @username или номер. Сообщения, файлы и даты остаются, поэтому в личном чате или «Избранном» автора всё равно видно."),
        ("¿Qué hace «Ocultar quién exportó el chat»?", "Quita del archivo lo que apunta a tu cuenta: tus reacciones y votos en encuestas, tus marcas de lectura y, por defecto, los nombres y fotos de los participantes, que se sustituyen por @usuario o un número. Los mensajes, archivos y fechas se conservan, así que en un chat privado o en Mensajes guardados se sigue viendo quién hizo la exportación.")),
    "anonymous_keep": (
        ("Can an anonymous export keep people's names and photos?", "Yes: once “Hide who exported the chat” is on, “Keep people's names” and “Keep profile photos” appear. They are off by default: Telegram shows your contacts under the names and photos you gave them, and some photos only to contacts, so these can show whose export it is."),
        ("Можно ли оставить в анонимном архиве имена и фото участников?", "Да: после включения «Скрыть автора экспорта» появятся «Оставить имена людей» и «Оставить фото профилей». По умолчанию они выключены: контакты Telegram показывает под именами и с фото, которые дали им вы, а некоторые фото видны только контактам, — по ним можно понять, чей это экспорт."),
        ("¿Puede una exportación anónima conservar los nombres y fotos de las personas?", "Sí: al activar «Ocultar quién exportó el chat» aparecen «Conservar los nombres» y «Conservar las fotos de perfil». Están desactivadas por defecto: Telegram muestra tus contactos con los nombres y fotos que les diste, y algunas fotos solo a los contactos, así que pueden revelar de quién es la exportación.")),
    "limit": (
        ("What does “Maximum file size” mean?", "Files larger than this are not downloaded, but their messages stay in the archive. The limit applies to each file, not to the whole archive."),
        ("Что значит «Максимальный размер файла»?", "Файлы крупнее этого размера не скачиваются, но сообщения с ними остаются в архиве. Ограничение действует на каждый файл отдельно, а не на весь архив."),
        ("¿Qué significa «Tamaño máximo por archivo»?", "Los adjuntos más grandes no se descargan, pero sus mensajes se conservan. El límite se aplica a cada adjunto, no a toda la exportación.")),
    "split": (
        ("Why split the archive into parts?", "Smaller parts are easier to copy or send. Keep all parts of one export together and upload them to the site together."),
        ("Зачем делить архив на части?", "Небольшие части проще копировать и пересылать. Держите все части одного экспорта вместе и загружайте их на сайт вместе."),
        ("¿Para qué dividir el archivo en partes?", "Las partes pequeñas son más fáciles de copiar o enviar. Guarda juntas todas las partes de una exportación y súbelas al sitio a la vez.")),
    "archive_limit": (
        ("Why “Keep each archive within a size limit”?", "So that the SaveGram site accepts every archive. A very large chat then becomes several archives, each in its own folder: upload them one at a time. Turn it off if you want a single archive for yourself."),
        ("Зачем «Ограничить размер каждого архива»?", "Чтобы сайт SaveGram принял каждый архив. Очень большой чат тогда разойдётся на несколько архивов, каждый в своей папке, — загружайте их по одному. Выключите, если нужен один архив для себя."),
        ("¿Para qué «Limitar el tamaño de cada archivo»?", "Para que el sitio SaveGram acepte cada archivo. Un chat muy grande se convierte en varios archivos, cada uno en su carpeta: súbelos de uno en uno. Desactívalo si quieres un solo archivo para ti.")),
    "folder": (
        ("Where is the export saved?", "In the folder you choose: each export gets a new folder inside it. Make sure the disk has room: a large chat with media can take many gigabytes."),
        ("Куда сохраняется экспорт?", "В выбранную папку: для каждого экспорта в ней создаётся новая папка. Проверьте, что на диске хватит места, — большой чат с медиа может занять много гигабайт."),
        ("¿Dónde se guarda la exportación?", "En la carpeta que elijas: cada exportación crea dentro una carpeta nueva. Comprueba que haya espacio en el disco: un chat grande con archivos puede ocupar muchos gigabytes.")),
    "export": (
        ("What happens after “Create TGEX”?", "The app reads the chosen chat, downloads its files and puts everything into a TGEX archive in the output folder. The bar shows how much is done: messages read and files downloaded. Keep the app open and the computer online until it finishes: a large chat can take hours. If Telegram asks to wait, the app waits and continues on its own."),
        ("Что происходит после «Создать TGEX»?", "Программа читает выбранный чат, скачивает файлы и собирает архив TGEX в папке результата. Полоса показывает, сколько уже сделано: прочитанные сообщения и скачанные файлы. Не закрывайте программу и не отключайте интернет до конца — большой чат может выгружаться несколько часов. Если Telegram попросит подождать, программа подождёт и продолжит сама."),
        ("¿Qué pasa al pulsar «Crear TGEX»?", "La aplicación lee el chat elegido, descarga sus archivos y lo reúne todo en un archivo TGEX en la carpeta de destino. La barra muestra cuánto se ha hecho: mensajes leídos y archivos descargados. Mantén la aplicación abierta y el equipo conectado hasta terminar: un chat grande puede tardar horas. Si Telegram pide esperar, la aplicación espera y continúa sola.")),
    "telegram_request": (
        ("My phone shows a Telegram request to export data. What is it?", "It is SaveGram Exporter asking Telegram for its fast export mode, which Telegram confirms this way after a new sign-in; the app shows it before the export, as in the picture below. Press “Allow” in it on your phone, and the export reads history about twice as fast. You do not have to: the export goes ahead either way, at the usual speed."),
        ("На телефон пришёл запрос Telegram на экспорт данных — что это?", "Это SaveGram Exporter просит у Telegram быстрый режим выгрузки: после нового входа Telegram подтверждает его так, а программа показывает это сообщение перед выгрузкой — как на картинке ниже. Нажмите в нём «Allow» на телефоне, и история будет читаться примерно вдвое быстрее. Можно и не нажимать — выгрузка всё равно пройдёт, просто с обычной скоростью."),
        ("Mi teléfono muestra una solicitud de Telegram para exportar datos. ¿Qué es?", "Es SaveGram Exporter pidiendo a Telegram su modo de exportación rápida, que Telegram confirma así tras un inicio de sesión nuevo; la aplicación lo muestra antes de exportar, como en la imagen de abajo. Pulsa «Allow» en él desde tu teléfono y el historial se leerá casi el doble de rápido. No hace falta: la exportación sigue igualmente, a la velocidad habitual.")),
    "cancel": (
        ("What happens if I click “Cancel”?", "The export stops, but what is already downloaded is kept. To continue from the same place, start the export again with the same settings."),
        ("Что будет, если нажать «Отменить»?", "Экспорт остановится, но всё уже скачанное сохранится. Чтобы продолжить с того же места, запустите экспорт снова с теми же настройками."),
        ("¿Qué pasa si pulso «Cancelar»?", "La exportación se detiene, pero lo ya descargado se conserva. Para seguir desde el mismo punto, vuelve a iniciar la exportación con los mismos ajustes.")),
    "resume": (
        ("How do I continue an interrupted export?", "Sign in to the same account, load the chats and export the same chat again with the same dates and settings: the app continues where it stopped. You can also click “Resume saved export…” and pick checkpoint.sqlite in the .savegram-resume-… folder inside the output folder. That folder disappears once the export is finished; if you will not continue, you can delete it."),
        ("Как продолжить прерванный экспорт?", "Войдите в тот же аккаунт, загрузите чаты и снова запустите экспорт того же чата с теми же датами и настройками — программа продолжит с места остановки. Можно и нажать «Продолжить сохранённый экспорт…» и выбрать файл checkpoint.sqlite в папке .savegram-resume-… внутри папки результата. Эта папка исчезает сама, когда экспорт завершён; если продолжать не будете, её можно удалить."),
        ("¿Cómo continúo una exportación interrumpida?", "Inicia sesión con la misma cuenta, carga los chats y vuelve a exportar el mismo chat con las mismas fechas y ajustes: la aplicación sigue donde se quedó. También puedes pulsar «Reanudar exportación guardada…» y elegir checkpoint.sqlite en la carpeta .savegram-resume-… dentro de la carpeta de destino. Esa carpeta desaparece sola al terminar la exportación; si no vas a continuar, puedes borrarla.")),
    "result": (
        ("Where is the finished archive and how do I view it?", "Click “Open result folder”: the .tgex file (or several parts) is there. Open the SaveGram site and upload it: the app sends nothing by itself. If the result lists omissions, some files could not be obtained; everything else can be viewed as usual."),
        ("Где готовый архив и как его посмотреть?", "Нажмите «Открыть папку результата» — там файл .tgex (или несколько частей). Откройте сайт SaveGram и загрузите его: сама программа ничего не отправляет. Если в итогах указаны пропуски, часть файлов получить не удалось; всё остальное смотрится как обычно."),
        ("¿Dónde está el archivo terminado y cómo lo veo?", "Pulsa «Abrir carpeta del resultado»: ahí está el archivo .tgex (o varias partes). Abre el sitio SaveGram y súbelo: la aplicación no envía nada por sí sola. Si el resultado indica omisiones, algunos archivos no se pudieron obtener; lo demás se ve con normalidad.")),
    "logout": (
        ("How do I sign out?", "Click “Sign out and forget account”. The app signs out of Telegram and forgets the sign-in on this computer; your exports stay. You can also sign it out in Telegram: Settings → Devices → SaveGram Exporter."),
        ("Как выйти из аккаунта?", "Нажмите «Выйти и забыть аккаунт». Программа выйдет из Telegram и забудет вход на этом компьютере; готовые архивы останутся. Выйти можно и из самого Telegram: Настройки → Устройства → SaveGram Exporter."),
        ("¿Cómo cierro sesión?", "Pulsa «Salir y olvidar la cuenta». La aplicación cierra la sesión de Telegram y olvida el acceso en este equipo; tus exportaciones se conservan. También puedes cerrarla desde Telegram: Ajustes → Dispositivos → SaveGram Exporter.")),
    "forget": (
        ("What does “Forget local saved account” do?", "It erases the saved sign-in from this computer, for example if it no longer opens. Then sign in again with the QR code. Your exports are not touched. To sign out completely, also end it in Telegram: Settings → Devices."),
        ("Что делает «Удалить сохранённый аккаунт локально»?", "Стирает сохранённый вход с этого компьютера — например, если он перестал открываться. После этого войдите заново по QR-коду. Готовые архивы не затрагиваются. Чтобы выйти полностью, завершите вход и в Telegram: Настройки → Устройства."),
        ("¿Qué hace «Olvidar cuenta local guardada»?", "Borra el acceso guardado en este equipo, por ejemplo si ya no se abre. Después vuelve a entrar con el código QR. Tus exportaciones no se tocan. Para cerrar la sesión por completo, ciérrala también en Telegram: Ajustes → Dispositivos.")),
    "privacy": (
        ("Where does my data go?", "Only to your computer: the app gets the chat from Telegram and saves the archive here. It reaches the SaveGram site only if you upload it yourself. The app sends no messages and changes or deletes nothing in your chats. When you no longer need it, click “Sign out and forget account”."),
        ("Куда уходят мои данные?", "Только на ваш компьютер: программа получает переписку из Telegram и сохраняет архив у вас. На сайт SaveGram он попадёт, только если вы загрузите его сами. Программа не отправляет сообщений и ничего не меняет и не удаляет в ваших чатах. Когда она станет не нужна, нажмите «Выйти и забыть аккаунт»."),
        ("¿A dónde van mis datos?", "Solo a tu equipo: la aplicación obtiene el chat de Telegram y guarda el archivo aquí. Llega al sitio SaveGram solo si tú lo subes. La aplicación no envía mensajes ni cambia o borra nada en tus chats. Cuando ya no la necesites, pulsa «Salir y olvidar la cuenta».")),
    "source": (
        ("Can I check the program?", "Yes. Its source code is open: it is published at savegram.tech/source and comes with the program in the “source” folder, together with instructions for building it yourself (BUILD.md). Download the program only from savegram.tech."),
        ("Можно ли проверить программу?", "Да. Её исходный код открыт: он опубликован на savegram.tech/source и лежит в архиве с программой, в папке source, вместе с инструкцией, как собрать её самому (BUILD.md). Скачивайте программу только с savegram.tech."),
        ("¿Puedo comprobar el programa?", "Sí. Su código fuente es abierto: está publicado en savegram.tech/source y viene con el programa, en la carpeta source, junto con instrucciones para compilarlo tú mismo (BUILD.md). Descarga el programa solo de savegram.tech.")),
    "language": (
        ("What does the language setting change?", "The language of the app and this help. It starts in your Windows language. Chat messages are not translated."),
        ("Что меняет выбор языка?", "Язык программы и этой справки. Сначала он совпадает с языком Windows. Переписка не переводится."),
        ("¿Qué cambia el idioma?", "El idioma de la aplicación y de esta ayuda. Empieza en el idioma de Windows. Los mensajes del chat no se traducen.")),
}
# Shown only while the window asks for an API ID and API Hash.
SETUP_TOPICS = ("configuration", "app_keys")
# The addresses the help names, linked where they appear; the browser opens them when clicked.
LINKS = {"savegram.tech/source": "https://savegram.tech/source", "savegram.tech": "https://savegram.tech",
         "my.telegram.org": "https://my.telegram.org"}
_LINKED = re.compile("|".join(re.escape(name) for name in sorted(LINKS, key=len, reverse=True)))
# The answer just opened lights up twice, softly, over this long: the eye finds it without reading.
FLASH_MS = 1800
FLASH_ALPHA = 0.22
# Room around the text for that light, in the help's own pixels.
FAQ_MARGIN = 10
# Pictures beside an answer, by the name the help's HTML gives them, and how wide they show.
PICTURES = {"telegram_request": "takeout-request.png"}
PICTURE_WIDTH = 320

CONTROL_TOPICS = {
    "anonymize_exporter": "anonymize_exporter", "keep_names": "anonymous_keep", "keep_avatars": "anonymous_keep",
    "app_id": "app_keys", "app_hash": "app_keys",
    "login": "login", "qr_hint": "login", "remember": "remember", "twofa": "twofa", "submit": "twofa",
    "logout": "logout", "forget_local": "forget", "chats": "chats", "chat": "chats",
    "start": "dates", "end": "dates", "media": "media", "limit": "limit", "split": "split",
    "part_size": "split", "limit_archive": "archive_limit", "archive_limit": "archive_limit", "folder": "folder", "browse": "folder", "export": "export", "resume": "resume",
    "cancel": "cancel", "open": "result", "site": "result", "source": "source", "language": "language",
}


def plain(answer):
    """An answer's words, its steps one after another."""
    return answer if isinstance(answer, str) else " ".join(answer)


def linked(text):
    return _LINKED.sub(lambda match: f'<a href="{LINKS[match.group()]}">{match.group()}</a>', escape(text))


def shown_topics(setup=False):
    return [key for key in TOPICS if setup or key not in SETUP_TOPICS]


def faq_html(language, setup=False):
    """The help in `language`; the answers about an API ID and API Hash only while the window asks for them."""
    index = LANGUAGES.index(language)
    topics = [(key, TOPICS[key][index]) for key in shown_topics(setup)]
    parts = [f'<html><body><a name="top"></a><h1>{escape(TITLES[index])}</h1><ul>']
    for key, (question, _) in topics:
        parts.append(f'<li><a href="#{key}">{escape(question)}</a></li>')
    parts.append("</ul>")
    for key, (question, answer) in topics:
        parts.append(f'<a name="{key}"></a><h2>{escape(question)}</h2>')
        if isinstance(answer, str):
            parts.append(f"<p>{linked(answer)}</p>")
        else:
            parts.append("<ol>" + "".join(f"<li>{linked(step)}</li>" for step in answer) + "</ol>")
        if key in PICTURES:
            parts.append(f'<p><img src="{PICTURES[key]}"></p>')
    parts.append("</body></html>")
    return "".join(parts)


def anchor_names(block):
    """The anchors a block of the help starts or holds: a topic's name sits on its question's first letter."""
    names = set(block.charFormat().anchorNames())
    fragments = block.begin()
    while not fragments.atEnd():
        names.update(fragments.fragment().charFormat().anchorNames())
        fragments += 1
    return names


class FaqBrowser(QTextBrowser):
    """The help's text. The answer just opened lights up softly twice, as Telegram lights up a message it
    jumps to, so the eye finds it at once instead of reading for it."""

    def loadResource(self, kind, url):
        """The help's own pictures, from the program itself; nothing else is ever fetched. Scaled here, for
        the screen's pixels: the document's own scaling left the picture's small text ragged."""
        if kind == QTextDocument.ImageResource and url.toString() in PICTURES.values():
            ratio = self.devicePixelRatioF()
            image = QImage.fromData(TAKEOUT_REQUEST).scaledToWidth(round(PICTURE_WIDTH * ratio), Qt.SmoothTransformation)
            image.setDevicePixelRatio(ratio)
            return image
        return None

    def __init__(self):
        super().__init__()
        self.flash_topic = None
        self.flash = QVariantAnimation(self)
        self.flash.setStartValue(0.0)
        self.flash.setEndValue(1.0)
        self.flash.setDuration(FLASH_MS)
        self.flash.valueChanged.connect(lambda value: self.viewport().update())
        self.flash.finished.connect(self.viewport().update)

    def flash_answer(self, topic=None):
        """Light up `topic`'s answer from the start; none puts the light out."""
        self.flash.stop()
        self.flash_topic = topic
        if topic is None:
            self.viewport().update()
        else:
            self.flash.start()

    def flash_alpha(self):
        """How strong the light is now: two soft swells, dark before, between and after them."""
        if self.flash.state() == QAbstractAnimation.Stopped:
            return 0.0
        return FLASH_ALPHA * math.sin(2 * math.pi * self.flash.currentValue()) ** 2

    def topic_rect(self, topic):
        """The topic's question and answer, up to the next topic, in the viewport's coordinates."""
        document = self.document()
        layout = document.documentLayout()
        area, inside = QRectF(), False
        block = document.begin()
        while block.isValid():
            names = anchor_names(block)
            if inside and names.intersection(TOPICS):
                break
            inside = inside or topic in names
            if inside:
                area = area.united(layout.blockBoundingRect(block))
            block = block.next()
        if area.isEmpty():
            return None
        # As wide as the text column with a little room around, wherever the words of a line end.
        side = document.documentMargin() - 8
        return QRectF(side, area.top() - 6 - self.verticalScrollBar().value(),
                      self.viewport().width() - 2 * side, area.height() + 12)

    def paintEvent(self, event):
        alpha = self.flash_alpha()
        area = self.topic_rect(self.flash_topic) if alpha > 0 else None
        if area is not None:
            # Under the text: the document draws no background of its own over it.
            color = QColor(theme.COLORS["accent"])
            color.setAlphaF(alpha)
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(area, 10, 10)
            painter.end()
        super().paintEvent(event)


class FaqWindow(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        # Windows gives a dialog a "?" in its title bar for Qt's "What's This?" pointer. Nothing here has
        # such a text: it only changed the pointer until the next click.
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.resize(760, 640)
        self.topic = "top"
        self.language = "en"
        self.setup = False
        self.browser = FaqBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.anchorClicked.connect(self.follow)
        self.contents_button = QPushButton()
        self.contents_button.setProperty("kind", "link")
        self.contents_button.clicked.connect(lambda: self.open_topic("top"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.addWidget(self.contents_button, 0, Qt.AlignLeft)
        layout.addWidget(self.browser)

    def follow(self, url):
        """A question in the contents opens its answer; one of the help's addresses opens in the browser."""
        if url.toString() in LINKS.values():
            QDesktopServices.openUrl(url)
        else:
            self.open_topic(url.fragment())

    def set_language(self, language):
        self.language = language
        index = LANGUAGES.index(language)
        self.setWindowTitle(TITLES[index])
        self.contents_button.setText(("Contents", "К оглавлению", "Índice")[index])
        # Colours are part of the HTML's style: set again for the theme in use before every drawing.
        self.browser.document().setDefaultStyleSheet(theme.faq_css())
        self.browser.setHtml(faq_html(language, self.setup))
        self.browser.document().setDocumentMargin(FAQ_MARGIN)
        self.scroll_to(self.topic)

    def scroll_to(self, topic):
        # A little room above the question, for the light around it.
        self.browser.scrollToAnchor(topic)
        bar = self.browser.verticalScrollBar()
        bar.setValue(bar.value() - FAQ_MARGIN)

    def show_setup(self, shown):
        """The answers about an API ID and API Hash: there while the window asks for them, gone after."""
        if shown == self.setup:
            return
        self.setup = shown
        if self.topic not in shown_topics(shown):
            self.topic = "top"
        self.set_language(self.language)

    def open_topic(self, topic="top"):
        if topic not in shown_topics(self.setup) and topic != "top":
            return
        self.topic = topic
        self.show()
        self.raise_()
        self.scroll_to(topic)
        self.browser.flash_answer(None if topic == "top" else topic)

# wallpaper_auto_downloader
Wallpaper Engine - Wallpaper Scheduled Auto-Switch Application<br/>
Automatically download Workshop files via steamcmd and let WE app switch wallpapers on a schedule.<br/>
<br/>
Since Steam prohibits automated programs from performing headless login, and there is no API for auto-subscribing to Workshop content, the only option is to use steamcmd for downloading, not subscribing.<br/>
<br/>
The config requires the following parameters:<br/>
- api_key (Steam Web API key, obtainable at https://steamcommunity.com/dev/apikey)<br/>
- interval (scheduling period: e.g. 3h / 90m / 1h30m; leave empty to execute only once)<br/>
- filters (set according to your needs; to avoid malicious content, it is recommended to only use Scene and Video for types)<br/>
- we_exe (path to the Wallpaper Engine executable; in Steam: right-click → Manage → Browse local files)<br/>
- workshop_root (path where WE app wallpapers are stored; it is recommended not to use the official Workshop path to avoid deleting subscribed wallpapers when wallpapers are removed)<br/>
- steamcmd (path to steamcmd, must be downloaded first: https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip)<br/>
- steam_username (Steam account that owns Wallpaper Engine, required for login)<br/>
<br/>
Before using, extract steamcmd into a folder, and set the path to steamcmd.exe in the config. Then, in the tray icon menu, choose to log in to the account and follow the process.<br/>
<br/>
After starting the program, a tray icon will appear. Right-click to set whether the app runs at startup, or to switch wallpapers immediately. Left-click opens the console (to check if login and wallpaper download work properly). It is recommended not to switch wallpapers too frequently, as this may trigger Steam’s anti-bot mechanisms.<br/>
<br/>
Due to steamcmd being a standalone Steam login, the Steam client may require re-login after logging in, and during the program download process, Wallpaper Engine may not be able to subscribe properly.<br/>
<br/>
Wallpaper Engine - 壁纸定时自动切换应用程序<br/>
通过steamcmd自动下载创意工坊文件并让we应用来定时切换壁纸<br/>
因为steam禁止自动化程序进行无头登录，且也没有自动订阅创意工坊的api，所以只能依靠steamcmd下载，不能订阅<br/>
config需要输入参数:<br/>
api_key（steam的web api_key，在https://steamcommunity.com/dev/apikey 获取）<br/>
interval（定时周期：如 3h / 90m / 1h30m；留空则只执行一次）<br/>
filters内参数请自行判断所需内容，为了防止赛博花柳病，types建议只填入Scene, Video<br/>
we_exe（wallpaper engine的程序路径，在steam中右键，-管理-浏览本地文件查看）<br/>
workshop_root（让we应用的壁纸留存的路径，建议不使用官方的workshop路径，防止壁纸删除的时候删掉创意工坊订阅的壁纸）<br/>
steamcmd（steamcmd 路径,需要先下载steamcmd：https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip ）<br/>
steam_username：（需要登录的购买了wallpaper engine的账号）<br/>
使用之前，先解压steamcmd放入某个文件夹，config里填入该steamcmd.exe所在路径。然后在托盘图标菜单选择登录账号，然后按流程操作
程序启动后，会在应用托盘显示一个图标，右键可以设置应用是否开机自启，立即更换壁纸，左键为查看控制台（可以排查是否正常登录和下载了壁纸），建议不要更换壁纸太过频繁，可能触发steam反爬机制<br/>
由于steamcmd是独立的steam登录，登录账号后steam客户端可能需要重新登录，程序下载过程中wallpaper engine内可能无法正常订阅

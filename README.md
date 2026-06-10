# hy2-manager

轻量化 Hysteria2 多用户一键管理脚本，终端交互参考经典 SSR 数字菜单。

当前版本：`v1.1.15`

## 一键部署

推荐 Debian / Ubuntu，使用 root 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SSTAPAPP/hy2-manager/main/install.sh)
```
只安装管理脚本、不立即初始化 Hysteria2：

```bash
HY2_SKIP_CORE_INSTALL=1 bash <(curl -fsSL https://raw.githubusercontent.com/SSTAPAPP/hy2-manager/main/install.sh)
```

安装完成后运行：

```bash
hy2
```

## 主菜单

```text
 1. 安装 Hysteria2
 2. 更新 Hysteria2 内核
 3. 卸载 Hysteria2
————————————
 4. 用户管理
 5. 显示在线 IP 和地理位置
 6. 查看认证历史
 7. 清零流量
————————————
 8. 启动 Hysteria2
 9. 停止 Hysteria2
10. 重启 Hysteria2
11. 查看服务状态
————————————
12. 其他功能
13. 健康检查
 0. 退出
```

## 功能

用户管理：

- 添加、删除、修改用户
- 启用 / 禁用用户
- 查看完整节点信息
- 限制用户设备数
- 限制用户总流量
- 设置每日 / 每周 / 每月流量清零
- 设置用户到期时间，到期自动禁用并踢下线

连接与流量：

- 显示在线 IP、中文地理位置和网络类型
- 查看认证历史
- 清零单个用户或全部用户流量

服务与系统：

- 安装、更新、卸载 Hysteria2
- 启动、停止、重启服务
- 查看服务状态和日志
- 安装 / 启用 BBR
- 数据库备份与恢复
- 健康检查

## 常用命令

```bash
hy2                         # 打开主菜单
hy2 client-config 用户名     # 查看指定用户节点信息
hy2 online                  # 查看在线连接和最近 IP
hy2 auth-history            # 查看认证历史
hy2 doctor                  # 健康检查
hy2 restart                 # 重启 hy2-auth / hysteria / monitor
```

## 目录

- 项目目录：`/opt/hy2-manager`
- 配置目录：`/etc/hy2-manager`
- Hysteria 配置：`/etc/hysteria/config.yaml`
- 数据库备份：`/etc/hy2-manager/backups`
- 管理入口：`/usr/local/bin/hy2`

## 设计说明

- 服务端固定监听 `443/udp`。
- 没有自有域名时使用自签证书，客户端 URI 会启用 `insecure=1`，默认 SNI 为 `www.bing.com`。
- `salamander` 混淆密码安装时默认随机生成，也可以在系统设置中改为自定义值；修改后旧节点会全部失效，需要重新复制节点信息。
- 下载限速写入用户节点 URI 的 `downmbps` 参数，用于客户端侧平滑限速；不按设备数平均切分，避免多设备体验被切碎。
- 上传固定为无限制，不提供新增或修改入口。
- 服务端负责用户启用状态、设备数、总流量、到期时间、流量清零和在线统计。
- 在线 IP 使用中文地理位置展示；国内显示省 / 市 / 区县和网络类型，海外统一显示未知。
- 终端输出统一高亮用户名、IP、端口、状态、限速、流量、URI、服务状态和版本号等关键信息。
- systemd 单元启用开机自启，并使用 `NoNewPrivileges`、`PrivateTmp`、`ProtectHome`、`ProtectControlGroups`、`ProtectKernelModules` 和 `RestrictSUIDSGID` 做轻量加固。

## 维护

```bash
hy2 doctor
```

健康检查会覆盖 root 权限、文件权限、systemd 状态、开机自启、UDP 443、认证后端、统计接口、SQLite 完整性、BBR/fq 和磁盘使用率。

更新项目代码：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/SSTAPAPP/hy2-manager/main/install.sh)
```

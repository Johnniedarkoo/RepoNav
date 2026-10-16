# Third-party notices

This source-only snapshot contains RepoNav's orchestration, adapters, documentation and synthetic validation tools. Repository evidence did not identify vendored third-party implementation code. The adapter calls Graphify's installed APIs; it does not copy its extraction, merge or clustering implementation. The owner confirms the necessary rights to distribute and license RepoNav's original code under MIT; see [LICENSE](LICENSE).

No third-party binaries, source distributions, dependency environments or generated HTML are bundled. The following tools are installed independently by users; these credits do not claim ownership or relicense them.

| Project | Relationship | License and upstream evidence |
| --- | --- | --- |
| Serena (`serena-agent`) 1.7.0 | Required external CLI; its installed Python environment also supplies YAML parsing | [MIT, v1.7.0 LICENSE](https://github.com/oraios/serena/blob/v1.7.0/LICENSE) |
| Graphify (`graphifyy`) 0.9.72 | Required external CLI and installed Python APIs | [Apache-2.0, v0.9.72 LICENSE](https://github.com/Graphify-Labs/graphify/blob/v0.9.72/LICENSE); the installed distribution retains LICENSE-MIT and NOTICE for earlier MIT contributions |
| ripgrep (`rg`) | Required external executable | [MIT OR Unlicense](https://github.com/BurntSushi/ripgrep/blob/master/COPYING) |
| uv | Required external tool-environment discovery and user-directed provisioning | [MIT OR Apache-2.0](https://github.com/astral-sh/uv#license) |
| Git for Windows / Git | Required external repository operations | [Git GPL-2.0](https://github.com/git/git/blob/master/COPYING); [Git for Windows](https://gitforwindows.org/) includes separately licensed components |
| Microsoft Pyright | Language-specific Python server used by Serena; 1.1.403 is the inspected default, not an executed RepoNav integration result | [MIT](https://github.com/microsoft/pyright/blob/main/LICENSE.txt) |
| pyright-python | Independently resolved Python wrapper used by Serena's default Pyright command; can provision Node | [MIT, package metadata](https://pypi.org/project/pyright/1.1.403/) |

Serena 1.7.0 is the last MIT release. [Serena's licensing overview](https://github.com/oraios/serena/blob/main/LICENSE) states that v2 and later application code is GPL-3.0-or-later, while SolidLSP remains MIT. RepoNav supports only 1.7.0; this notice does not broaden compatibility. Executing an independently installed GPL program does not by itself relicense RepoNav's original code.

Python runtimes and adapter-specific servers (including Node-based servers) are provisioned separately. AI clients and Serena MCP connections are optional, independently configured software. Their licenses and settings apply separately. Transitive dependency license inventories and upstream license texts are not bundled because those packages are not redistributed here. Redistributing tools or copying upstream implementation later requires a fresh notice and licensing review.

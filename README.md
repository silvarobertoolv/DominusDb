# SQLBridge — SQL Server → MySQL

Aplicação web local em Python 3.11+ com Flask, SQLAlchemy, pymssql (FreeTDS) e PyMySQL. Preparada para catálogo do **SQL Server 2008 (10.0) / 2008 R2 (10.50)** e destino **MySQL 8**. Não usa ODBC nem precisa publicar um site. A compatibilidade com seu servidor deve ser confirmada com uma migração de teste: não foi possível acessar bancos reais neste ambiente.

## Instalação no Windows

Instale Python 3.11 ou 3.12 de 64 bits. No PowerShell, dentro da pasta do projeto:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Abra **http://127.0.0.1:8000** e mantenha o terminal aberto. Para encerrar, Ctrl+C. Não é necessário ativar o ambiente virtual. Depois de instalar, você também pode usar `iniciar.bat`.

## Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Os pacotes normalmente oferecem wheels. Caso não exista wheel para seu sistema/Python, a compilação de pymssql exige FreeTDS e ferramentas de desenvolvimento. Prefira Python 3.11/3.12 de 64 bits.

## Preparação dos bancos

- SQL Server: habilite TCP/IP no SQL Server Configuration Manager, confirme a porta real da instância e use autenticação SQL Server. Instâncias nomeadas podem usar uma porta diferente de 1433: informe o host e a porta separados, sem `\\INSTANCIA` no campo servidor.
- Conta de origem: SELECT nas tabelas escolhidas e VIEW DEFINITION para refletir objetos e ler definições. Visibilidade de catálogo depende das permissões. Objetos criptografados retornam definição nula. A aplicação só lê a origem.
- MySQL: crie previamente um banco **exclusivo e vazio**. Use uma conta com CREATE, INSERT, SELECT, ALTER, INDEX e REFERENCES nesse banco. Exemplo de criação pelo administrador:

```sql
CREATE DATABASE migracao CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
```

- Se os bancos estiverem em outra máquina, libere a porta no firewall e confirme o acesso a partir da máquina que executa o Python.
- SQL Server 2008: configure TLS 1.2 com os patches correspondentes quando possível. A conexão usa TDS 7.3 e exige criptografia por padrão. Se um servidor legado em rede confiável não negociar TLS, a opção de criptografia pode ser desmarcada conscientemente; nesse caso, os dados trafegam sem criptografia. Windows Authentication/SSO não está incluído nesta versão.
- Para MySQL remoto, marque TLS. O certificado deve ser confiável para o Python e corresponder ao nome do servidor. Sem essa opção, a aplicação não exige TLS no MySQL.

## Fluxo

1. Teste e conecte a origem e o destino. As senhas são removidas dos campos após uma conexão bem-sucedida, mas os engines mantêm as credenciais na memória.
2. Clique em **Ler catálogo** e selecione as tabelas.
3. Gere o plano. Revise tipos, nomes, DDL, bloqueios e avisos; baixe o ZIP com o plano e as definições originais.
4. Pause as escritas na origem. Não há snapshot global, CDC nem sincronização incremental: mudanças concorrentes podem produzir uma cópia inconsistente entre tabelas.
5. Digite **MIGRAR** e execute. O progresso informa linhas enviadas; o relatório confirma linhas efetivamente gravadas por tabela.
6. Baixe o relatório, valide contagens, valores e regras de negócio. A contagem do destino é comparada às linhas copiadas, sem checksum de conteúdo. Depois adapte e valide os objetos manuais antes de direcionar sua aplicação ao MySQL.

## Escopo automático

- Descoberta de tabelas, colunas, PKs, índices, UNIQUEs, FKs e CHECKs.
- Conversão dos tipos comuns; bloqueio de tipos que exigem decisão manual, como `datetimeoffset`, tipos espaciais e tipos desconhecidos.
- Criação das tabelas e PKs/UNIQUEs, cópia em lotes configuráveis (1 a 10.000 linhas), criação de índices comuns e FKs entre as tabelas selecionadas depois da carga.
- Preservação dos valores originais das identidades, sem recriar o gerador IDENTITY/AUTO_INCREMENT.
- Nome de destino `schema__tabela`. Nomes de tabela/índice acima de 64 caracteres são abreviados com hash; colisões entre nomes de tabelas são bloqueadas. Nomes de coluna acima de 64 caracteres bloqueiam o plano.
- `rowversion/timestamp` do SQL Server torna-se binário de 8 bytes; não vira data/hora. `money` torna-se NUMERIC(19,4), `uniqueidentifier` torna-se VARCHAR(36), tipos de texto grandes tornam-se LONGTEXT.
- Exportação das definições originais de views, procedures, funções e triggers em JSON. Sequences e synonyms são inventariados, mas suas propriedades específicas precisam ser extraídas/adaptadas separadamente.

## Adaptação manual e limites

Views, procedures, funções e triggers **não são convertidos nem executados automaticamente**. T-SQL, funções nativas, tratamento de erros, SQL dinâmico, tabelas temporárias e transações precisam de reescrita para MySQL. Esta versão entrega o inventário e as definições disponíveis para essa etapa.

Defaults, IDENTITY/AUTO_INCREMENT, expressões de colunas calculadas e CHECKs não são recriados; os valores das colunas são copiados. Índices filtrados, com INCLUDE ou columnstore exigem revisão. FKs para tabelas fora da seleção são omitidas e sinalizadas. FKs com SET DEFAULT bloqueiam o plano. Triggers não são ativadas durante a carga porque não são criadas.

Revise collation, comparação com espaços, acentos, maiúsculas e minúsculas, regras de UNIQUE com NULL, comprimento de índices, limite de linha do InnoDB, chaves sobre tipos TEXT/BLOB e diferenças de precisão temporal (MySQL até 6 casas; SQL Server até 7). Algumas incompatibilidades podem ser detectadas apenas na execução pelo MySQL; o plano não certifica equivalência de comportamento. A conexão SQL Server 2008 será validada no ambiente real.

A aplicação **não apaga, trunca ou reutiliza tabelas existentes**. Se qualquer tabela planejada já existir, recusa a operação. DDL do MySQL faz commit implícito: uma falha pode deixar tabelas vazias ou parciais. Cada carga de tabela é uma transação; tabelas anteriores permanecem confirmadas. Índices e FKs são criados em seguida. Não existe retomada automática: após falha, inspecione o relatório e use um novo banco vazio ou remova manualmente apenas os objetos da tentativa anterior. Não mate o processo durante uma migração.

Uso previsto: uma pessoa e uma migração por vez, na máquina local. Estado, plano, conexões e histórico ficam em memória e se perdem ao encerrar o processo. Baixe os relatórios antes de fechar. O serviço é limitado a 127.0.0.1, valida Host e usa proteção CSRF; não o exponha na rede como serviço multiusuário. Mensagens de drivers não são mostradas, pois podem conter SQL, credenciais ou dados de negócio. Falhas mostram etapa e classe do erro. Não há cancelamento nesta versão.

## Testes

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```

No Windows, substitua `.venv/bin/python` por `.venv\Scripts\python.exe`. Os testes verificam conversões de tipos, fluxo HTTP, bloqueio CSRF, preservação de tabelas existentes, cópia em lotes e contagens usando SQLite, e compilação de DDL MySQL. Não substituem testes integrados SQL Server 2008 → MySQL 8, que exigem servidores acessíveis e credenciais.

## Diagnóstico de falhas no plano

Se a geração do plano falhar, a interface mantém um quadro com referência do erro, etapa, objeto e mensagem original do driver (quando disponível). A mesma mensagem é registrada no terminal. Copie esse diagnóstico ao relatar uma falha: ele distingue erros de catálogo, reflexão, conversão e compilação do DDL. Uma conta administradora não resolve falhas de compatibilidade ou de metadados. Credenciais conhecidas da origem são ocultadas e a exceção SQLAlchemy não é exibida com SQL e parâmetros. O diagnóstico pode conter nomes de objetos; revise antes de compartilhar.

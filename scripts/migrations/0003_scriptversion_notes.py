# Generated by Django 3.2.10 on 2021-12-12 17:45

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scripts', '0002_alter_scriptversion_tags'),
    ]

    operations = [
        migrations.AddField(
            model_name='scriptversion',
            name='notes',
            field=models.TextField(blank=True),
        ),
    ]
